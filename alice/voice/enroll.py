""""Sesimi tanıt" ("Enroll my voice") in the app: the UI version of `alice voice enroll`.

On the command line the sentence to read was not shown, and it was unclear whether the mic heard it. Here the
recording runs on the server (the computer with the mic) in a worker thread. The app reads view() often and shows
the sentence to read, the mic level, which sentence it is on, and the model download progress. When it finishes,
a short test is run. The running core notices the new voice print by itself (SpeakerGate watches the file), so no
restart is needed. While enrolling, the "Hey Alice, saat kaç?" ("Hey Alice, what time is it?") read aloud is covered
by the enrolling() marker so it does not wake ALICE.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from alice.i18n import _
from alice.voice import speaker_id as sid

LISTEN_SECONDS = 15.0  # wait for one sentence; if nothing is said, "duyamadım" ("I couldn't hear")
TRIES = 3  # the same sentence is requested at most this many times
MIN_TAKES = 4  # if fewer sentences than this are captured, the profile is not saved
MAX_TAKE = 7.0  # s: sentences are short; if background sound keeps the take from ending, it is cut here


class Cancelled(Exception):
    pass


def _make_mic():
    from alice.voice.audio import Microphone

    return Microphone()


def _make_vad():
    from alice.voice.listen import VAD

    return VAD()


class EnrollSession:
    """One enrollment at a time. start()/test()/install() run a worker thread; cancel() stops; view() gives state."""

    def __init__(self, mic: Callable[[], Any] = _make_mic, vad: Callable[[], Any] = _make_vad,
                 embedder: Callable[[], Any] | None = None, pip: Callable[[], int] | None = None,
                 listen_seconds: float = LISTEN_SECONDS, got_pause: float = 0.8):
        self.make_mic, self.make_vad = mic, vad
        self.make_embedder = embedder
        self.pip = pip or self._pip_install
        self.listen_seconds = listen_seconds
        self.got_pause = got_pause
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._done = threading.Event()  # "Okudum" ("I've read it"): take what has been recorded so far
        self._skip = threading.Event()  # "Bu cümleyi atla" ("skip this sentence")
        self._thread: threading.Thread | None = None
        self._s: dict[str, Any] = {}
        self._reset("idle")

    # ------------------------------------------------------------------ state
    def _reset(self, state: str, **kw: Any) -> None:
        with self._lock:
            self._s = {"state": state, "step": 0, "total": len(sid.enroll_sentences()), "sentence": "", "level": 0.0,
                       "hearing": False, "took": 0.0, "left": 0, "message": "", "progress": None, "result": None,
                       "takes": [], "got": "", **kw}

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._s.update(kw)

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def view(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._s, takes=list(self._s["takes"]))
        prof = sid.load_profile()
        out["available"] = sid.available()
        out["model"] = sid.model_path().is_file()
        out["profile"] = ({"samples": prof.get("samples"), "threshold": prof.get("threshold"),
                           "consistency": prof.get("consistency"), "created": prof.get("created")} if prof else None)
        return out

    # ------------------------------------------------------------------ actions
    def _run(self, name: str, fn: Callable[[], None]) -> dict[str, Any]:
        if self.running():
            return {"ok": False, "error": _("already running")}
        self._cancel.clear()

        def body() -> None:
            try:
                fn()
            except Cancelled:
                self._reset("cancelled", message="iptal edildi")
            except Exception as e:  # noqa: BLE001 - show it on screen, don't crash the app
                self._set(state="error", message=str(e) or type(e).__name__, level=0.0, hearing=False)
            finally:
                try:
                    sid.enrolling_path().unlink()
                except OSError:
                    pass  # marker already gone

        self._thread = threading.Thread(target=body, daemon=True, name=f"alice-voice-{name}")
        self._thread.start()
        return {"ok": True}

    def start(self) -> dict[str, Any]:
        self._reset("preparing", message=_("preparing"))
        return self._run("enroll", self._enroll)

    def test(self) -> dict[str, Any]:
        if sid.load_profile() is None:
            return {"ok": False, "error": _("enroll your voice first")}
        self._reset("preparing", message=_("preparing"))
        return self._run("enroll-test", self._test_only)

    def install(self) -> dict[str, Any]:
        self._reset("installing", message=_("installing sherpa-onnx (it may take a minute)"))
        return self._run("enroll-install", self._install)

    def done(self) -> dict[str, Any]:
        self._done.set()
        return {"ok": True}

    def skip(self) -> dict[str, Any]:
        self._skip.set()
        return {"ok": True}

    def cancel(self) -> dict[str, Any]:
        self._cancel.set()
        return {"ok": True}

    def remove(self) -> dict[str, Any]:
        if self.running():
            return {"ok": False, "error": _("enrollment is in progress")}
        try:
            sid.profile_path().unlink()
        except FileNotFoundError:
            pass  # no profile yet: nothing to delete
        self._reset("idle", message=_("voice recognition turned off: ALICE answers everyone's voice again"))
        return {"ok": True}

    # ------------------------------------------------------------------ steps
    @staticmethod
    def _pip_install() -> int:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.run([sys.executable, "-m", "pip", "install", "-q", "sherpa-onnx"], capture_output=True,
                              timeout=600, creationflags=flags).returncode

    def _install(self) -> None:
        if self.pip() != 0:
            raise RuntimeError(_("could not install sherpa-onnx; try in a terminal: pip install sherpa-onnx"))
        importlib.invalidate_caches()
        if not sid.available():
            raise RuntimeError(_("sherpa-onnx was installed but could not be loaded; restart the app"))
        self._reset("idle", message="sherpa-onnx kuruldu")

    def _prepare(self) -> Any:
        if not sid.available():
            raise RuntimeError(_("sherpa-onnx is not installed"))
        if not sid.model_path().is_file():
            self._set(state="downloading", message=_("downloading the voice recognition model (~40 MB, once)"), progress=0.0)

            def progress(done: int, total: int) -> None:
                if self._cancel.is_set():
                    raise Cancelled
                self._set(progress=(done / total) if total else None)

            sid.download_model(progress=progress)
        self._set(state="preparing", message=_("loading the model"), progress=None)
        return (self.make_embedder or sid.Embedder)()

    def _touch_marker(self) -> None:
        path = sid.enrolling_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def _record(self, mic: Any, vad: Any) -> Any:
        """Record one sentence; None if the time runs out or it is skipped. The screen is updated every frame: whether
        it is listening, whether it hears, how many seconds have passed, how much time is left.

        End of sentence: a short silence, OR the level dropped well below the speech level (while Discord/game audio
        is on, VAD never reports "silence", and the recording ran 12 s with nothing changing on screen), OR MAX_TAKE
        seconds have passed, OR the user pressed "Okudum" ("I've read it")."""
        from alice.voice.audio import Audio, rms

        frame_s = 0.03
        start_frames, end_frames, quiet_frames = 5, int(0.6 / frame_s), int(0.7 / frame_s)
        t0 = time.monotonic()
        touched = 0.0
        ring: list[bytes] = []  # all frames since the sentence was shown (used when "Okudum" is pressed)
        buf: list[bytes] | None = None
        voiced = silent = quiet = 0
        peak = 0.0
        self._done.clear()
        self._skip.clear()
        self._set(hearing=False, took=0.0, left=round(self.listen_seconds), level=0.0)
        while True:
            if self._cancel.is_set():
                raise Cancelled
            if self._skip.is_set():
                self._set(hearing=False, level=0.0)
                return None
            frame = mic.read()
            now = time.monotonic()
            if now - touched > 5:
                touched = now
                self._touch_marker()
            level = rms(frame)
            ring.append(frame)
            ring = ring[-int(MAX_TAKE / frame_s):]
            speech = vad.is_speech(frame, 16000)
            if buf is None:
                voiced = voiced + 1 if speech else 0
                if voiced >= start_frames:
                    buf = ring[-(start_frames + 10):]  # don't cut off the start of the speech
                    peak, silent, quiet = level, 0, 0
            else:
                buf.append(frame)
                peak = max(peak, level)
                silent = 0 if speech else silent + 1
                quiet = quiet + 1 if level < peak * 0.25 else 0
            took = len(buf) * frame_s if buf is not None else 0.0
            self._set(level=round(min(1.0, level * 6), 3), hearing=buf is not None, took=round(took, 1),
                      left=max(0, round(self.listen_seconds - (now - t0))))
            if self._done.is_set():  # "Okudum": take the audio up to that moment
                pcm = b"".join(buf if buf is not None and took >= 1.0 else ring)
                self._done.clear()
                if len(pcm) / 32000 >= 1.0 and max((rms(f) for f in (buf or ring)), default=0) > 0.01:
                    return Audio(pcm, 16000)
                self._set(message="ses gelmedi, bir daha oku")
                continue
            if buf is not None and took >= 1.0 and (silent >= end_frames or quiet >= quiet_frames or took >= MAX_TAKE):
                self._set(hearing=False, level=0.0)
                return Audio(b"".join(buf), 16000)
            if buf is None and now - t0 > self.listen_seconds:
                self._set(hearing=False, level=0.0)
                return None

    def _try_out(self, mic: Any, vad: Any, emb: Any, prof: dict[str, Any]) -> None:
        self._set(state="testing", sentence="", message=_("Now say a sentence of your own, let's try."))
        audio = self._record(mic, vad)
        if audio is None:
            self._set(state="done", message=_("no sound for the test; your voice was saved anyway"))
            return
        gate = sid.SpeakerGate(prof, emb)
        ok = gate.check(audio.pcm, audio.rate)
        with self._lock:
            prev = self._s.get("result") or {}
        self._set(state="done", result={**prev, "score": round(gate.last_score, 3), "threshold": gate.threshold,
                                        "recognized": ok},
                  message=(_("it recognised you") if ok else _("it did not recognise you: try enrolling again somewhere quiet")))

    def _enroll(self) -> None:
        emb = self._prepare()
        self._set(message=_("opening the microphone"))
        vad = self.make_vad()
        sentences = sid.enroll_sentences()
        embeddings: list[list[float]] = []
        with self.make_mic() as mic:
            for i, sentence in enumerate(sentences, 1):
                for attempt in range(TRIES):
                    self._set(state="recording", step=i, sentence=sentence, got="",
                              message=_("read it") if attempt == 0 else _("I couldn't hear, read it again"))
                    audio = self._record(mic, vad)
                    if self._skip.is_set():
                        self._skip.clear()
                        break
                    if audio is not None:
                        embeddings.append(emb.embed(audio.pcm, audio.rate))
                        with self._lock:
                            self._s["takes"].append(round(audio.seconds, 1))
                        # Show that it was taken on screen, then move to the next sentence.
                        self._set(got=_("✓ got it ({n} s)", n=f"{audio.seconds:.1f}"), hearing=False, level=0.0)
                        time.sleep(self.got_pause)
                        break
            if len(embeddings) < MIN_TAKES:
                raise RuntimeError(_("not enough recordings: the microphone may not be hearing you (pick the right "
                                     "microphone in the Windows sound settings)"))
            prof = sid.save_profile(embeddings)
            self._set(result={"samples": prof["samples"], "threshold": prof["threshold"],
                              "consistency": prof["consistency"], "noisy": prof["consistency"] < 0.55})
            self._try_out(mic, vad, emb, prof)

    def _test_only(self) -> None:
        prof = sid.load_profile()
        if prof is None:
            raise RuntimeError(_("enroll your voice first"))
        emb = self._prepare()
        with self.make_mic() as mic:
            self._try_out(mic, self.make_vad(), emb, prof)
