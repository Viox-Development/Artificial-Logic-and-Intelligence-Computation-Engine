"""Speaker: queues sentences and plays the previous one while the next one is being synthesized.

stop() empties the queue and cuts the sound being played at once (when the user interrupts). Synthesis and
playback run in separate threads, so speech starts as soon as the first sentence is ready.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable

from alice.voice.audio import Audio, Player
from alice.voice.tone import drop_emoji, for_speech, pick


def scaled(audio: Audio, volume: float) -> Audio:
    """Lower the volume (0-1). 1 or above: unchanged."""
    if volume >= 0.999 or not audio.pcm:
        return audio
    import array

    a = array.array("h")
    a.frombytes(audio.pcm[: len(audio.pcm) - len(audio.pcm) % 2])
    v = max(0.0, volume)
    for i in range(len(a)):
        a[i] = int(a[i] * v)
    return Audio(a.tobytes(), audio.rate)


class Speaker:
    def __init__(self, tts, player: Player | None = None, on_error: Callable[[str], None] = lambda s: None,
                 enabled: Callable[[], bool] = lambda: True):
        self.tts = tts
        self.player = player or Player()
        self.on_error = on_error
        self.enabled = enabled
        # Volume level (0-1): speech and flight/game alerts are separate. In War Thunder the alerts were shouting over
        # the game sound; [voice] volume / alert_volume.
        self.volume = 1.0
        self.alert_volume = 0.5
        self._gen = 0
        self._synth_q: queue.Queue = queue.Queue()
        self._play_q: queue.Queue = queue.Queue()
        self._pending = 0
        self._cv = threading.Condition()
        self._playing = False
        self._now = ""  # sentence being played
        self._now_alert = False  # whether the sound being played is a flight/game alert
        self._last_alert = False  # whether the last item added to the queue was an alert
        self.alerts_muted = False  # separate silent mode for game/flight alerts
        self._making: tuple[int, str] | None = None  # sentence being synthesized (not in any queue yet)
        # What could not be said at the last stop(): {"playing": sentence cut off midway, "pending": never said}
        self.last_cut: dict[str, object] | None = None
        self._expect: tuple[float, Callable[[float], None]] | None = None  # to measure the delay of the first sound
        threading.Thread(target=self._synth_loop, daemon=True, name="alice-tts").start()
        threading.Thread(target=self._play_loop, daemon=True, name="alice-play").start()

    # ------------------------------------------------------------------ public
    def expect(self, since: float | None, on_first: Callable[[float], None] | None = None) -> None:
        """When the next sound starts playing, call on_first(seconds since `since`); since None: cancel."""
        with self._cv:
            self._expect = (since, on_first) if since is not None and on_first is not None else None

    # A flight announcement is dropped if it cannot start playing within this many seconds: "Düşman saat 2"
    # ("enemy at 2 o'clock") is wrong half a minute later.
    STALE = {"critical": 12.0, "warn": 10.0, "info": 8.0}
    STALE_LONG = 45.0  # long information (landing assessment)

    def say(self, text: str, emotion: str | None = None, severity: str = "", topic: str = "") -> bool:
        """Add to the queue. False if sound is off (e.g. silent mode)."""
        text = for_speech(text)
        emotion = emotion or pick(text, severity, topic)  # emoji (😄) sets the tone, then it is removed
        text = drop_emoji(text)
        if not text or not self.enabled() or (topic == "flight" and self.alerts_muted):
            return False
        deadline = None
        if topic == "flight":
            ttl = self.STALE_LONG if len(text) > 90 else self.STALE.get(severity, self.STALE["info"])
            deadline = time.monotonic() + ttl
            if severity in ("critical", "warn"):
                self._drop_queued_flight_info()  # urgent alert first: queued minor announcements are dropped
        with self._cv:
            self._pending += 1
            gen = self._gen
        self._last_alert = topic == "flight"
        self._synth_q.put((gen, text, emotion, deadline, severity if topic == "flight" else ""))
        return True

    def _drop_queued_flight_info(self) -> None:
        kept, dropped = [], 0
        try:
            while True:
                item = self._synth_q.get_nowait()
                if item[4] == "info":
                    dropped += 1
                else:
                    kept.append(item)
        except queue.Empty:
            pass  # queue drained
        for item in kept:
            self._synth_q.put(item)
        if dropped:
            with self._cv:
                self._pending = max(0, self._pending - dropped)
                self._cv.notify_all()

    def stop_alerts(self) -> None:
        """Cut the game/flight alert being played; do not affect the speech queue."""
        # Queued items are skipped in the synth/play loops via alerts_muted; the one playing is cut here.
        if self._playing and self._now_alert:
            self.player.stop()

    def stop(self) -> None:
        """Stop talking: empty the queue and cut what is playing. What could not be said is written to last_cut."""
        with self._cv:
            gen = self._gen
            self._gen += 1
            self._pending = 0
            playing = self._now if self._playing else ""
            self._cv.notify_all()
        making = self._making
        pending: list[tuple[int, str]] = []
        # Order of speech: items waiting to be played, the one being synthesized, items still in the synthesis queue.
        for q in (self._play_q, None, self._synth_q):  # items are (gen, text, ...)
            if q is None:
                pending += [making] if making is not None else []
                continue
            try:
                while True:
                    item = q.get_nowait()
                    pending.append((item[0], item[1]))
            except queue.Empty:
                pass  # queue drained
        self.player.stop()
        rest = [t for g, t in pending if g == gen and isinstance(t, str)]
        self.last_cut = {"playing": playing, "pending": rest} if playing or rest else None

    def prewarm(self, items: list[tuple[str, str]], limit: int = 12, log: Callable[[str], None] = lambda s: None,
                 background: bool = True) -> int:
        """Pre-generate the audio of frequently spoken fixed sentences (it is cached and plays instantly later).
        At most `limit` per call, so the quota does not run out at once; the rest is completed on later startups.
        It waits while speaking or in flight (GPU busy in a game, voice from the cloud) and is not done then.
        Returns the count made."""
        cached = getattr(self.tts, "cached", None)
        if cached is None:
            return 0

        def run() -> int:
            made = 0
            for text, emotion in items:
                if made >= limit:
                    break
                text = drop_emoji(for_speech(text))
                policy = getattr(self.tts, "policy", None)
                if callable(policy) and policy().get("local_gpu") is False:
                    break
                try:
                    if cached(text, emotion):
                        continue
                    while self.speaking:
                        time.sleep(1)
                    self.tts.synth(text, emotion)
                except Exception as e:  # noqa: BLE001 - no quota/engine: continue on the next startup
                    log(f"could not make a ready-made sentence: {e}")
                    break
                made += 1
            if made:
                log(f"{made} ready-made sentences cached")
            return made

        if not background:
            return run()
        threading.Thread(target=run, daemon=True, name="alice-tts-prewarm").start()
        return 0

    @property
    def speaking(self) -> bool:
        with self._cv:
            return self._playing or self._pending > 0

    @property
    def alerting(self) -> bool:
        """Is a game/flight alert being spoken right now (not ALICE's chat reply)."""
        with self._cv:
            if self._playing:
                return self._now_alert
            return self._pending > 0 and self._last_alert

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until everything to be said is finished."""
        with self._cv:
            return self._cv.wait_for(lambda: self._pending == 0 and not self._playing, timeout)

    # ---------------------------------------------------------------- threads
    def _done_one(self, gen: int, n: int = 1) -> None:
        with self._cv:
            if gen == self._gen and self._pending > 0:
                self._pending = max(0, self._pending - n)
            self._cv.notify_all()

    MERGE_CHARS = 300  # queued sentences are synthesized in one go (separate requests left gaps between them)

    def _merge_queued(self, gen: int, text: str, emotion: str) -> tuple[str, int]:
        """During synthesis, append queued sentences with the same tone (not flight announcements) to this one."""
        n = 1
        held = []
        try:
            while len(text) < self.MERGE_CHARS:
                item = self._synth_q.get_nowait()
                fits = len(text) + len(item[1]) <= self.MERGE_CHARS
                if item[0] != gen or item[2] != emotion or item[3] is not None or not fits:
                    held.append(item)
                    break
                text, n = f"{text} {item[1]}", n + 1
        except queue.Empty:
            pass  # no more sentences to merge
        if held:  # put back without breaking the order
            rest = []
            try:
                while True:
                    rest.append(self._synth_q.get_nowait())
            except queue.Empty:
                pass  # queue drained
            for item in held + rest:
                self._synth_q.put(item)
        return text, n

    def _synth_loop(self) -> None:
        while True:
            gen, text, emotion, deadline, _sev = self._synth_q.get()
            if gen != self._gen:
                continue
            if deadline is not None and (self.alerts_muted or time.monotonic() > deadline):
                self._done_one(gen)  # stale or alerts muted: do not synthesize at all
                continue
            n = 1
            if deadline is None:
                text, n = self._merge_queued(gen, text, emotion)
            self._making = (gen, text)
            try:
                audio: Audio | None = self.tts.synth(text, emotion)
            except Exception as e:
                self.on_error(f"could not make the audio: {e}")
                audio = None
            finally:
                self._making = None
            if gen != self._gen:
                continue
            if audio is None:
                self._done_one(gen, n)
                continue
            self._play_q.put((gen, text, audio, deadline, n, _sev))

    def _play_loop(self) -> None:
        while True:
            gen, text, audio, deadline, n, sev = self._play_q.get()
            if gen != self._gen:
                continue
            if deadline is not None and (self.alerts_muted or time.monotonic() > deadline + 3):
                self._done_one(gen, n)  # went stale while the sound was being synthesized, or alerts muted
                continue
            with self._cv:
                self._playing = True
                self._now = text
                self._now_alert = bool(sev)
                first, self._expect = self._expect, None
            if first is not None:
                try:
                    first[1](time.monotonic() - first[0])
                except Exception:  # noqa: BLE001 - the measurement must not break the sound
                    pass
            try:
                self.player.play(scaled(audio, self.alert_volume if sev else self.volume))
            except Exception as e:
                self.on_error(f"could not play the audio: {e}")
            finally:
                with self._cv:
                    self._playing = False
                self._done_one(gen, n)
