"""Ayarlar → Çekirdek: arka plandaki ALICE ve sesli asistan, komut satırına gerek kalmadan.

`alice core status|install|uninstall|mode`, `alice voice on|off|test|status` burada düğme olur. Çekirdek ayrı bir
süreç: başlatma start_detached ile, durdurma ~/.alice/core-stop isteğiyle (çekirdek bir saniye içinde kapanır;
eski sürüm bu isteği tanımıyorsa süreç sonlandırılır).
"""

from __future__ import annotations

import importlib.util
import threading
import time
from typing import Any, Callable

from alice.core import service

MODES = ("auto", "normal", "dev", "flight", "quiet")
STOP_WAIT = 15.0  # çekirdek bu sürede kapanmazsa süreç sonlandırılır
OPTIONAL = (("sounddevice", "mikrofon ve ses çalma"), ("pynput", "bas-konuş ve kısayollar"),
            ("webrtcvad", "daha iyi konuşma algılama"), ("faster_whisper", "yerel Whisper (Groq yoksa)"),
            ("openwakeword", "yerel 'Hey Alice'"), ("sherpa_onnx", "sadece senin sesine cevap"),
            ("pystray", "tepsi simgesi"))


def _events(limit: int = 12) -> list[dict[str, Any]]:
    from alice.core.store import Store

    try:
        with Store() as st:
            evs = st.events(limit=limit)
    except Exception:  # noqa: BLE001 - veritabanı kilitli/bozuk: panel yine açılsın
        return []
    return [{"ts": e.ts, "kind": e.kind, "title": e.title, "body": e.body[:300], "severity": e.severity} for e in evs]


def stop_core(wait: float = STOP_WAIT, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Çalışan çekirdeği kapat. Kapandıysa True."""
    import psutil

    st = service.read_state()
    if not st.get("running"):
        return True
    service.request_stop()
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not service.read_state().get("running"):
            return True
        sleep(0.3)
    try:  # eski sürüm core-stop'u tanımıyor: süreci kapat
        p = psutil.Process(int(st["pid"]))
        p.terminate()
        p.wait(5)
    except (psutil.Error, KeyError, ValueError, TypeError):
        pass
    try:
        service.state_path().unlink()
    except OSError:
        pass
    service.release_lock_of(st.get("pid"))
    try:
        service.stop_path().unlink()
    except OSError:
        pass
    return True


class MicTest:
    """5 saniye dinle, seviyeyi canlı göster, sonra duyulanı yazıya çevir (`alice voice test`)."""

    def __init__(self, cfg: Any, mic: Callable[[], Any] | None = None, stt: Callable[[Any], Any] | None = None,
                 seconds: float = 5.0):
        self.cfg = cfg
        self.make_mic = mic
        self.make_stt = stt
        self.seconds = seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._s: dict[str, Any] = {"state": "idle", "level": 0.0, "peak": 0.0, "left": 0.0, "text": "", "engine": "",
                                   "message": ""}

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._s.update(kw)

    def view(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._s)

    def start(self) -> dict[str, Any]:
        if self._thread is not None and self._thread.is_alive():
            return {"ok": False, "error": "test sürüyor"}
        self._set(state="listening", level=0.0, peak=0.0, left=self.seconds, text="", engine="", message="")
        self._thread = threading.Thread(target=self._run, daemon=True, name="alice-mic-test")
        self._thread.start()
        return {"ok": True}

    def _run(self) -> None:
        from alice.voice.audio import Audio, Microphone, rms

        try:
            mic = (self.make_mic or Microphone)()
        except ModuleNotFoundError as e:
            self._set(state="error", message=f"{e.name or 'sounddevice'} kurulu değil")
            return
        except Exception as e:  # noqa: BLE001
            self._set(state="error", message=f"mikrofon açılamadı: {e}")
            return
        frames: list[bytes] = []
        peak = 0.0
        try:
            with mic:
                t0 = time.monotonic()
                while (left := self.seconds - (time.monotonic() - t0)) > 0:
                    f = mic.read()
                    frames.append(f)
                    lvl = rms(f)
                    peak = max(peak, lvl)
                    self._set(level=round(min(1.0, lvl * 6), 3), peak=round(peak, 4), left=round(left, 1))
        except Exception as e:  # noqa: BLE001
            self._set(state="error", message=f"mikrofon okunamadı: {e}")
            return
        self._set(state="transcribing", level=0.0, left=0.0)
        if peak < 0.004:
            self._set(state="done", message="mikrofondan ses gelmiyor: Windows ses ayarlarında doğru mikrofon seçili mi, "
                                            "sesi kapalı mı?")
            return
        from alice.voice.stt import STTError, make_stt

        try:
            stt = (self.make_stt or make_stt)(self.cfg)
            text = stt.transcribe(Audio(b"".join(frames), 16000))
            self._set(state="done", text=text, engine=str(getattr(stt, "last", "") or ""),
                      message="" if text.strip() else "ses geldi ama konuşma anlaşılmadı")
        except STTError as e:
            self._set(state="done", message=f"yazıya çevrilemedi: {e}")


class SystemPanel:
    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.mic_test = MicTest(cfg)
        self._busy = ""  # sürmekte olan çekirdek işlemi (başlatılıyor / durduruluyor)
        self._lock = threading.Lock()

    def view(self) -> dict[str, Any]:
        from alice.voice.audio import mic_available

        st = service.read_state()
        v = self.cfg.data.get("voice", {})
        stt = []
        try:
            from alice.voice.stt import make_stt

            stt = [e.name for e in make_stt(self.cfg).engines]
        except Exception:  # noqa: BLE001
            pass
        return {
            "core": {"running": bool(st.get("running")), "pid": st.get("pid"), "mode": st.get("mode"),
                     "forced": st.get("forced"), "voice": st.get("voice"), "busy": self._busy,
                     "autostart": service.autostart_path().exists()},
            "voice": {"enabled": bool(v.get("enabled")), "mic": mic_available(), "stt": stt,
                      "wake_phrase": bool(v.get("wake_phrase", True)), "ptt_key": str(v.get("ptt_key", "") or "")},
            "packages": [{"name": m, "why": why, "ok": importlib.util.find_spec(m) is not None} for m, why in OPTIONAL],
            "events": _events(),
            "modes": list(MODES),
        }

    def _background(self, label: str, fn: Callable[[], None]) -> dict[str, Any]:
        with self._lock:
            if self._busy:
                return {"ok": False, "error": f"{self._busy}, bekle"}
            self._busy = label

        def run() -> None:
            try:
                fn()
            finally:
                self._busy = ""

        threading.Thread(target=run, daemon=True, name="alice-core-action").start()
        return {"ok": True}

    def action(self, action: str, d: dict[str, Any]) -> dict[str, Any]:
        from alice.config import set_config_value

        if action == "start":
            return self._background("başlatılıyor", lambda: (service.start_detached(), time.sleep(3)))
        if action == "stop":
            return self._background("durduruluyor", lambda: stop_core())
        if action == "restart":
            return self._background("yeniden başlatılıyor",
                                    lambda: (stop_core(), service.start_detached(), time.sleep(3)))
        if action == "autostart":
            if d.get("on"):
                try:
                    service.install_autostart()
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": f"kurulamadı: {e}"}
            else:
                service.uninstall_autostart()
            return {"ok": True}
        if action == "mode":
            mode = str(d.get("mode", ""))
            if mode not in MODES:
                return {"ok": False, "error": f"bilinmeyen mod: {mode}"}
            service.force_mode(None if mode == "auto" else mode)
            return {"ok": True}
        if action == "voice":
            on = bool(d.get("on"))
            self.cfg.data.setdefault("voice", {})["enabled"] = on
            set_config_value("voice", "enabled", on)
            # Sesli asistan çekirdeğin içinde: geçerli olması için çekirdek yeniden başlar (kapalıysa açarken başlar).
            running = service.read_state().get("running")
            if not running and not on:
                return {"ok": True}
            return self._background("yeniden başlatılıyor" if running else "başlatılıyor",
                                    lambda: (stop_core(), service.start_detached(), time.sleep(3)))
        if action == "mic_test":
            return self.mic_test.start()
        return {"ok": False, "error": f"bilinmeyen işlem: {action}"}
