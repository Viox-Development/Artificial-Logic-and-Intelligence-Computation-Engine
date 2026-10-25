"""Uygulamadan "Sesimi tanıt": `alice voice enroll`'un ekranlı hâli.

Komut satırında okunacak cümle görünmüyor, mikrofonun duyup duymadığı belli olmuyordu. Burada kayıt sunucuda
(mikrofonun olduğu bilgisayarda) bir iş parçacığında yürür; uygulama view()'i sık sık okuyup okunacak cümleyi,
mikrofon seviyesini, kaçıncı cümlede olunduğunu ve model indirmesinin ilerlemesini gösterir. Bitince kısa bir
deneme yapılır. Çalışan çekirdek yeni ses izini kendisi fark eder (SpeakerGate dosyayı izler), yeniden başlatmak
gerekmez. Tanıtma sürerken okunan "Hey Alice, saat kaç?" ALICE'i uyandırmasın diye enrolling() işareti tutulur.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from alice.voice import speaker_id as sid

LISTEN_SECONDS = 15.0  # bir cümle için bekleme; konuşulmazsa "duyamadım"
TRIES = 3  # aynı cümle en fazla bu kadar istenir
MIN_TAKES = 4  # bu kadar cümle alınamazsa profil kaydedilmez


class Cancelled(Exception):
    pass


def _make_mic():
    from alice.voice.audio import Microphone

    return Microphone()


def _make_vad():
    from alice.voice.listen import VAD

    return VAD()


class EnrollSession:
    """Tek seferde bir tanıtma. start()/test()/install() iş parçacığı başlatır, cancel() durdurur, view() durum."""

    def __init__(self, mic: Callable[[], Any] = _make_mic, vad: Callable[[], Any] = _make_vad,
                 embedder: Callable[[], Any] | None = None, pip: Callable[[], int] | None = None,
                 listen_seconds: float = LISTEN_SECONDS):
        self.make_mic, self.make_vad = mic, vad
        self.make_embedder = embedder
        self.pip = pip or self._pip_install
        self.listen_seconds = listen_seconds
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._s: dict[str, Any] = {}
        self._reset("idle")

    # ------------------------------------------------------------------ durum
    def _reset(self, state: str, **kw: Any) -> None:
        with self._lock:
            self._s = {"state": state, "step": 0, "total": len(sid.ENROLL_SENTENCES), "sentence": "", "level": 0.0,
                       "hearing": False, "message": "", "progress": None, "result": None, "takes": [], **kw}

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

    # ------------------------------------------------------------------ işlemler
    def _run(self, name: str, fn: Callable[[], None]) -> dict[str, Any]:
        if self.running():
            return {"ok": False, "error": "zaten sürüyor"}
        self._cancel.clear()

        def body() -> None:
            try:
                fn()
            except Cancelled:
                self._reset("cancelled", message="iptal edildi")
            except Exception as e:  # noqa: BLE001 - ekranda göster, uygulama çökmesin
                self._set(state="error", message=str(e) or type(e).__name__, level=0.0, hearing=False)
            finally:
                try:
                    sid.enrolling_path().unlink()
                except OSError:
                    pass

        self._thread = threading.Thread(target=body, daemon=True, name=f"alice-voice-{name}")
        self._thread.start()
        return {"ok": True}

    def start(self) -> dict[str, Any]:
        self._reset("preparing", message="hazırlanıyor")
        return self._run("enroll", self._enroll)

    def test(self) -> dict[str, Any]:
        if sid.load_profile() is None:
            return {"ok": False, "error": "önce sesini tanıt"}
        self._reset("preparing", message="hazırlanıyor")
        return self._run("enroll-test", self._test_only)

    def install(self) -> dict[str, Any]:
        self._reset("installing", message="sherpa-onnx kuruluyor (bir dakika sürebilir)")
        return self._run("enroll-install", self._install)

    def cancel(self) -> dict[str, Any]:
        self._cancel.set()
        return {"ok": True}

    def remove(self) -> dict[str, Any]:
        if self.running():
            return {"ok": False, "error": "tanıtma sürüyor"}
        try:
            sid.profile_path().unlink()
        except FileNotFoundError:
            pass
        self._reset("idle", message="ses tanıma kapatıldı: ALICE yine herkesin sesine cevap verir")
        return {"ok": True}

    # ------------------------------------------------------------------ adımlar
    @staticmethod
    def _pip_install() -> int:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.run([sys.executable, "-m", "pip", "install", "-q", "sherpa-onnx"], capture_output=True,
                              timeout=600, creationflags=flags).returncode

    def _install(self) -> None:
        if self.pip() != 0:
            raise RuntimeError("sherpa-onnx kurulamadı; komut satırında dene: pip install sherpa-onnx")
        importlib.invalidate_caches()
        if not sid.available():
            raise RuntimeError("sherpa-onnx kuruldu ama yüklenemedi; uygulamayı yeniden başlat")
        self._reset("idle", message="sherpa-onnx kuruldu")

    def _prepare(self) -> Any:
        if not sid.available():
            raise RuntimeError("sherpa-onnx kurulu değil")
        if not sid.model_path().is_file():
            self._set(state="downloading", message="ses tanıma modeli indiriliyor (~40 MB, bir kez)", progress=0.0)

            def progress(done: int, total: int) -> None:
                if self._cancel.is_set():
                    raise Cancelled
                self._set(progress=(done / total) if total else None)

            sid.download_model(progress=progress)
        self._set(state="preparing", message="model yükleniyor", progress=None)
        return (self.make_embedder or sid.Embedder)()

    def _record(self, mic: Any, vad: Any) -> Any:
        """Bir cümle kaydet. Süre dolarsa None. Her karede seviye güncellenir, iptale bakılır."""
        from alice.voice.audio import rms
        from alice.voice.listen import utterances

        end = time.monotonic() + self.listen_seconds
        touched = [0.0]

        def frames():
            while True:
                if self._cancel.is_set():
                    raise Cancelled
                frame = mic.read()
                now = time.monotonic()
                if now - touched[0] > 5:
                    touched[0] = now
                    path = sid.enrolling_path()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                self._set(level=round(min(1.0, rms(frame) * 6), 3))
                yield frame

        self._set(hearing=False)
        for audio in utterances(frames(), vad, end_silence=0.8, max_seconds=12, stop=lambda: time.monotonic() > end,
                                on_start=lambda: self._set(hearing=True)):
            if audio.seconds >= 1.0:
                self._set(hearing=False, level=0.0)
                return audio
            self._set(hearing=False)
        self._set(hearing=False, level=0.0)
        return None

    def _try_out(self, mic: Any, vad: Any, emb: Any, prof: dict[str, Any]) -> None:
        self._set(state="testing", sentence="", message="Şimdi kendi aklından bir cümle söyle, deneyelim.")
        audio = self._record(mic, vad)
        if audio is None:
            self._set(state="done", message="deneme için ses gelmedi; sesin yine de kaydedildi")
            return
        gate = sid.SpeakerGate(prof, emb)
        ok = gate.check(audio.pcm, audio.rate)
        with self._lock:
            prev = self._s.get("result") or {}
        self._set(state="done", result={**prev, "score": round(gate.last_score, 3), "threshold": gate.threshold,
                                        "recognized": ok},
                  message=("seni tanıdı" if ok else "tanımadı: sessiz bir yerde yeniden tanıtmayı dene"))

    def _enroll(self) -> None:
        emb = self._prepare()
        self._set(message="mikrofon açılıyor")
        vad = self.make_vad()
        sentences = sid.ENROLL_SENTENCES
        embeddings: list[list[float]] = []
        with self.make_mic() as mic:
            for i, sentence in enumerate(sentences, 1):
                for attempt in range(TRIES):
                    self._set(state="recording", step=i, sentence=sentence,
                              message="oku" if attempt == 0 else "duyamadım, bir daha oku")
                    audio = self._record(mic, vad)
                    if audio is not None:
                        embeddings.append(emb.embed(audio.pcm, audio.rate))
                        with self._lock:
                            self._s["takes"].append(round(audio.seconds, 1))
                        break
            if len(embeddings) < MIN_TAKES:
                raise RuntimeError("yeterli kayıt alınamadı: mikrofon sesini duymuyor olabilir "
                                   "(Windows ses ayarlarından doğru mikrofonu seç)")
            prof = sid.save_profile(embeddings)
            self._set(result={"samples": prof["samples"], "threshold": prof["threshold"],
                              "consistency": prof["consistency"], "noisy": prof["consistency"] < 0.55})
            self._try_out(mic, vad, emb, prof)

    def _test_only(self) -> None:
        prof = sid.load_profile()
        if prof is None:
            raise RuntimeError("önce sesini tanıt")
        emb = self._prepare()
        with self.make_mic() as mic:
            self._try_out(mic, self.make_vad(), emb, prof)
