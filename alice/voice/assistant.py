"""Sesli asistan döngüsü: dinle -> yazıya çevir -> ALICE -> konuş.

- Bas-konuş tuşu her zaman çalışır; "Hey Alice" el gerektirmez.
- ALICE cevap verdikten sonra birkaç saniye boyunca "Hey Alice" demeden devam edilebilir.
- ALICE konuşurken araya girersen susar, yarım kalan işi durdurur ve seni dinler.
- Cevap akarken ilk cümle hazır olur olmaz söylenir; araç çalışacaksa önce "hemen bakıyorum" der.
"""

from __future__ import annotations

import queue
import random
import re
import threading
import time
from typing import Any, Callable

from alice.tools.base import ToolResult
from alice.voice.audio import Audio, Microphone
from alice.voice.listen import FOLLOW_MAX, VAD, PushToTalk, WakeWord, is_hush, is_yes, strip_wake, utterances
from alice.voice.speaker import Speaker
from alice.voice.stt import STTError, is_hallucination
from alice.voice.tone import SentenceSplitter

VOICE_PROMPT = """# Voice mode
You are speaking out loud through text-to-speech. The user hears you, they may not be looking at the screen.
- Answer in Turkish, in 1-3 short spoken sentences unless asked for detail. No markdown, lists, tables or emojis.
- Each user message ends with the current date and time in brackets. Use it for time/date questions (say the time exactly as its "söylenişi" part);
  answer simple questions (time, date, small talk, general knowledge) directly without calling tools.
- Long-term memory is already in your system prompt: do not call the memory tool just to read it. When the user tells
  you something lasting about themselves (likes, dislikes, people, habits, how you should behave) save it with
  memory action=add; for "forget that" use action=forget; for past conversations not in your prompt use action=search.
- Weather: call the weather tool once (never run_command/curl for it).
- Never read code, paths or long numbers aloud; say you put it on screen.
- In urgent situations (flight warnings, errors) be clear and brief, no jokes.
- Checklists (checklist tool): read only the current step, then wait; on "tamam", "devam" or "sonraki" call next.
- "Not al" during a flight goes to the kneeboard (kneeboard tool, action note); otherwise to the notes tool."""

def with_clock(text: str) -> str:
    """Sesli asistan saatlerce açık kalır; sistem istemindeki saat eskir. Saati/tarihi her cümleye ekle ki
    "saat kaç" gibi sorular komut çalıştırmadan cevaplansın."""
    from datetime import datetime

    days = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    now = datetime.now()
    return (f"{text}\n\n[şu an: {now:%d.%m.%Y} {days[now.weekday()]}, saat {now:%H:%M}, "
            f"söylenişi: {spoken_time(now.hour, now.minute)}]")


NUMS = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz", "on", "on bir", "on iki"]
TENS = {10: "on", 20: "yirmi"}
ACC = {"bir": "biri", "iki": "ikiyi", "üç": "üçü", "dört": "dördü", "beş": "beşi", "altı": "altıyı", "yedi": "yediyi",
       "sekiz": "sekizi", "dokuz": "dokuzu", "on": "onu"}
DAT = {"bir": "bire", "iki": "ikiye", "üç": "üçe", "dört": "dörde", "beş": "beşe", "altı": "altıya", "yedi": "yediye",
       "sekiz": "sekize", "dokuz": "dokuza", "on": "ona"}


def _num(n: int) -> str:
    return NUMS[n] if n <= 12 else " ".join(w for w in (TENS[n // 10 * 10], NUMS[n % 10]) if w)


def _case(word: str, table: dict[str, str]) -> str:
    head, _, last = word.rpartition(" ")
    return f"{head} {table[last]}".strip()


def spoken_time(hour: int, minute: int) -> str:
    """Model "01:13"ü "bire on üç" diye yanlış okuyordu; saatin Türkçe söylenişini hazır ver."""
    def h12(h: int) -> str:
        return _num(h % 12 or 12)

    if minute == 0:
        return f"saat tam {h12(hour)}"
    if minute == 30:
        return f"saat {h12(hour)} buçuk"
    if minute < 30:
        return f"saat {_case(h12(hour), ACC)} {'çeyrek' if minute == 15 else _num(minute)} geçiyor"
    rest = 60 - minute
    return f"saat {_case(h12(hour + 1), DAT)} {'çeyrek' if rest == 15 else _num(rest)} var"


FILLERS = ["Hemen bakıyorum.", "Bir saniye, bakıyorum.", "Tamam, bakıyorum."]
LIVE_LIMIT_WAIT = 3600.0  # Live kotası dolunca normal sesli yolda kalma süresi (sn)


class VoiceUI:
    """Ajanın UI'ı: metni iç UI'a (terminal/uygulama) iletir, aynı zamanda seslendirir."""

    def __init__(self, speaker: Speaker, inner=None, ask: Callable[[str], bool] | None = None):
        self.speaker = speaker
        self.inner = inner
        self.ask = ask
        self.splitter = SentenceSplitter()
        self.spoke = False
        self.muted = False  # araya girildi: bu turun geri kalanı söylenmez
        self.models: list[str] = []
        self.tools: list[str] = []

    def _say(self, text: str, emotion: str | None = None) -> None:
        if not self.muted:
            self.spoke = self.speaker.say(text, emotion) or self.spoke

    def mute(self) -> None:
        self.muted = True
        self.speaker.stop()

    def _inner(self, name: str, *args: Any) -> Any:
        fn = getattr(self.inner, name, None) if self.inner is not None else None
        return fn(*args) if fn else None

    def begin_turn(self) -> None:
        self.splitter = SentenceSplitter()
        self.spoke = False
        self.muted = False
        self.models, self.tools = [], []

    def stream_start(self) -> None:
        self._inner("stream_start")

    def stream_delta(self, text: str) -> None:
        self._inner("stream_delta", text)
        for s in self.splitter.feed(text):
            self._say(s)

    def thinking_delta(self, text: str) -> None:
        self._inner("thinking_delta", text)

    def stream_reset(self) -> None:
        self._inner("stream_reset")
        self.speaker.stop()
        self.splitter = SentenceSplitter()

    def stream_end(self, text: str, source: str) -> None:
        self._inner("stream_end", text, source)
        self.models.append(source)
        rest = self.splitter.flush()
        if not self.spoke and not rest and text:
            # Akış yoktu (stream kapalı ya da yedek model): metnin tamamını söyle.
            rest = SentenceSplitter().feed(text + "\n") or [text]
        for s in rest:
            self._say(s)
        self.splitter = SentenceSplitter()

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self._inner("tool_start", call_id, name, summary)
        self.tools.append(f"{name}({summary[:80]})")
        if not self.spoke and not self.speaker.speaking:
            self._say(random.choice(FILLERS), "calm")

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        self._inner("tool_end", call_id, name, result)

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        if self.inner is not None and self.ask is None:
            return self.inner.ask_permission(name, summary, risk)
        if self.ask is None:
            return "n"
        return "y" if self.ask(f"{summary[:120]} için onay veriyor musun?") else "n"

    def info(self, msg: str) -> None:
        self._inner("info", msg)


def voice_log(line: str) -> None:
    """~/.alice/voice.log: ne duyuldu, ne cevap verildi (yanlış anlama sorunlarını görmek için)."""
    from datetime import datetime

    from alice.config import alice_home

    try:
        p = alice_home() / "voice.log"
        if p.exists() and p.stat().st_size > 2_000_000:
            p.replace(p.with_suffix(".log.1"))
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass


def trim_history(messages: list[dict[str, Any]], keep_turns: int) -> list[dict[str, Any]]:
    """Sistem istemi + son keep_turns kullanıcı turu (araç çağrıları bölünmeden). Sesli asistan saatlerce açık
    kalıyor; tüm geçmiş her istekte gidince eski konulara sapıyor ve küçük bağlamlı hızlı modellere sığmıyor."""
    starts = [i for i, m in enumerate(messages) if i and m.get("role") == "user" and isinstance(m.get("content"), str)
              and not m.get("_image")]
    if len(starts) <= keep_turns:
        return messages
    return messages[:1] + messages[starts[-keep_turns]:]


class VoiceAssistant:
    def __init__(self, agent, speaker: Speaker, stt, mic_factory: Callable[[], Microphone] = Microphone,
                 ptt_key: str = "", wake_model: str = "", wake_phrase: bool = True, barge_in: bool = True,
                 follow_up: float = FOLLOW_MAX, emit: Callable[[str], None] = lambda s: None,
                 keep_turns: int = 4, idle_reset: float = 300.0, live=None, end_silence: float = 0.5,
                 live_text_min: float | None = -0.7, live_text_wait: float = 1.5, live_follow: float = FOLLOW_MAX,
                 full_text_max: float = 8.0, live_gate: bool = True):
        self.agent = agent
        # İlk cümlenin yerel dökümü bu güvenin (ortalama log olasılık) üstündeyse Live'a ses yerine yazı gider.
        self.live_text_min = live_text_min
        self.live_text_wait = live_text_wait
        self.live_follow = min(live_follow, FOLLOW_MAX)  # canlı konuşma bitince "Alice" demeden devam süresi
        self.full_text_max = full_text_max  # bu uzunluğa kadar cümle uyandırmada bir kerede yazıya dökülür
        self._pre_text: tuple[Any, bool] = (None, False)
        from alice.voice.listen import SpeechGate

        self.live_gate: SpeechGate | None = SpeechGate() if live_gate else None
        self.end_silence = end_silence  # konuşma bitti saymak için sessizlik (sn)
        self.live = live  # Gemini Live: "Hey Alice"ten sonra sesten sese canlı konuşma
        if live is not None:
            live.gated = live_gate  # Live'a giden kareler süzülüyor: ses varsa gerçekten konuşma var
        self._live_q: queue.Queue | None = None  # doluyken mikrofon kareleri Live'a gider
        self._live_down = 0.0
        self.keep_turns = keep_turns
        self.idle_reset = idle_reset  # bu kadar saniye sessizlikten sonra yeni sohbet
        self._last_turn = 0.0
        self.speaker = speaker
        self.stt = stt
        self.mic_factory = mic_factory
        self.ptt_key = ptt_key
        self.wake_model = wake_model
        self.wake_phrase = wake_phrase
        self.barge_in = barge_in
        self.follow_up = min(follow_up, FOLLOW_MAX)
        self.muted = False  # "Alice sus": yeniden "Alice" denene kadar hiçbir şeyi komut sayma
        self._speech_at = 0.0  # son konuşmanın BAŞLADIĞI an (takip penceresi buna göre)
        self.emit = emit
        self.ui: VoiceUI = agent.ui if isinstance(agent.ui, VoiceUI) else VoiceUI(speaker, agent.ui)
        agent.ui = self.ui
        self.vad = VAD()
        self.stop_event = threading.Event()
        self.armed_until = 0.0      # bu ana kadar "Hey Alice" gerekmez
        self._jobs: queue.Queue = queue.Queue()
        self._answers: queue.Queue = queue.Queue()
        self._awaiting_answer = False
        self._busy = threading.Event()
        threading.Thread(target=self._worker, daemon=True, name="alice-voice-turns").start()

    # ---------------------------------------------------------------- olaylar
    def _interrupt(self) -> None:
        if self.speaker.speaking or self._busy.is_set():
            if self._awaiting_answer:
                self.speaker.stop()
                return
            self.ui.mute()
            if self._busy.is_set():
                self.agent.stop()
            self.emit("araya girildi, susuyorum")

    def _on_speech_start(self) -> None:
        self._speech_at = time.monotonic()
        if self.speaker.speaking and not self.muted:
            self.armed_until = max(self.armed_until, self._speech_at)  # Alice konuşurken söze girmek de ona söylenmiştir
        if self.barge_in and self.speaker.speaking:
            self._interrupt()

    def _armed(self, at: float | None = None) -> bool:
        """'Alice' demeden konuşulabilir mi? Konuşmanın başladığı ana bakılır: pencere içinde başlayıp dışında biten
        cümle sayılır, pencere kapandıktan sonra başlayan sayılmaz."""
        return not self.muted and (at or time.monotonic()) <= self.armed_until

    def _hush(self, why: str = "") -> None:
        """'Alice sus': konuşmayı kes, takip penceresini kapat, yeniden 'Alice' denene kadar dinleme."""
        self.muted = True
        self.armed_until = 0.0
        self.speaker.stop()
        if self._busy.is_set():
            self.ui.mute()
            self.agent.stop()
        voice_log(f"susturuldu{f': {why}' if why else ''} (yeniden 'Alice' denene kadar komut alınmıyor)")
        self.emit("sustum; 'Alice' diyene kadar dinlemiyorum")

    def ask_yes_no(self, question: str, timeout: float = 10) -> bool:
        """Sesli onay: soruyu söyle, sıradaki konuşmayı cevap say."""
        while not self._answers.empty():  # önceki sorudan kalan cevap bu soruyu onaylamasın
            try:
                self._answers.get_nowait()
            except queue.Empty:
                break
        self.speaker.say(question, "serious")
        self.speaker.wait(15)
        self._awaiting_answer = True
        self.armed_until = time.monotonic() + timeout
        try:
            ans = self._answers.get(timeout=timeout)
        except queue.Empty:
            return False
        finally:
            self._awaiting_answer = False
        return is_yes(ans)

    def handle_text(self, text: str, direct: bool) -> None:
        """Yazıya çevrilmiş konuşmayı işle (test edilebilir çekirdek)."""
        text = text.strip()
        if not text:
            return
        rest = strip_wake(text, strict=self.muted)
        armed = direct or self._armed()
        if is_hush(text) and (armed or rest is not None or self._awaiting_answer or self.speaker.speaking
                              or self._busy.is_set()):
            if self._awaiting_answer:
                self._answers.put("hayır")
            self._hush(text)
            return
        if self._awaiting_answer:
            self._answers.put(text)
            return
        if self.muted:
            if rest is None and not direct:
                return  # susturulmuş: sadece "Alice ..." ya da bas-konuş uyandırır
            self.muted = False
            voice_log("susturma kalktı")
        # Uyandırma kelimesi varsa her durumda at; yoksa sadece bas-konuş / takip penceresinde geçerli.
        if rest is not None and (armed or self.wake_phrase):
            text = rest
        elif not armed:
            return
        if not text:
            self.speaker.say("Efendim?", "calm")
            if self._live_ready():
                self._jobs.put("")  # canlı konuşmayı aç, kullanıcı söyleyecek
                return
            self.speaker.wait(5)  # pencere "Efendim?" bittikten sonra başlasın
            self.armed_until = time.monotonic() + self.follow_up
            return
        if self._busy.is_set():
            self.ui.mute()
            self.agent.stop()
        self._jobs.put(text)

    def handle_audio(self, audio: Audio, direct: bool, started: float | None = None) -> None:
        if audio.seconds < 0.3:
            return
        gate = getattr(self.stt, "wake_gate", None)
        if not direct and not self._awaiting_answer and gate is not None and self.wake_phrase:
            pre = self._live_ready() and not self._busy.is_set() and audio.seconds >= 0.6
            if pre:
                self.live.prefetch()  # bağlantı, uyandırma kontrolüyle aynı anda açılsın
            armed = self._armed(started)  # az önce cevap verdi: "Alice" demeden devam
            full = self._full_text(audio) if pre else None
            head = full[0] if full else gate.heard(audio)
            if head is not None and not head.strip() and not full:
                audio, head = self._scan_wake(gate, audio, head)
            conf = full[1] if full else getattr(gate, "last_conf", 0.0)
            # "Alice" demeden devam: gürültüden uydurulmuş ya da çok güvensiz döküm canlı konuşmayı yeniden açmasın.
            follow = (armed and len(head.strip()) > 1 and not is_hallucination(head) and conf > -1.2
                      if head is not None else False)
            woke = head is not None and (strip_wake(head, strict=self.muted) is not None or follow)
            if pre and not woke:
                self.live.discard()
            if head is not None and woke and is_hush(head):
                if pre:
                    self.live.discard()
                self._hush(head)  # "Alice sus" canlı konuşma açmasın
                return
            if head is not None and woke and self.muted:
                self.muted = False
                voice_log("susturma kalktı")
            if head is not None:
                if not woke:
                    if audio.seconds >= 0.6:
                        voice_log(f"uyandırma yok [{getattr(gate, 'model_name', '?')} {getattr(gate, 'last_seconds', 0):.2f}sn]: {head}")
                    return  # uyandırma yok: sesi buluta hiç gönderme
                direct = True  # yerel model "Hey Alice" duydu; bulut yazımı farklı olsa da kabul et
                if self._live_ready() and not self._busy.is_set():
                    # Canlı konuşma: yazıya çevirmeyi atla, cümlenin sesini doğrudan Live'a ver (~0,5 sn kazanç).
                    voice_log(f"uyandı [{getattr(gate, 'model_name', '?')} {getattr(gate, 'last_seconds', 0):.2f}sn]: {head}")
                    self._live_q = queue.Queue()  # bağlanırken söylenenler kaybolmasın: kareler şimdiden biriksin
                    self._pre_text = (full, armed and strip_wake(head) is None)
                    self._jobs.put(audio)
                    return
        try:
            text = self.stt.transcribe(audio)
        except STTError as e:
            self.emit(f"anlaşılamadı: {e}")
            return
        self.emit(f"duyulan: {text}")
        if is_hallucination(text):
            voice_log(f"duyulan (gürültü sayıldı) [{getattr(self.stt, 'last', '')}]: {text}")
            return
        voice_log(f"duyulan [{getattr(self.stt, 'last', '')}]{' (doğrudan)' if direct else ''}: {text}")
        self.handle_text(text, direct)

    def _scan_wake(self, gate, audio: Audio, head: str, step: float = 2.0, limit: int = 5) -> tuple[Audio, str]:
        """Cümlenin başı konuşma değilse (oyun/müzik sesiyle başlayan uzun kayıt) "Hey Alice" ileride olabilir:
        sonraki pencerelere bak, bulursa kaydı oradan başlat. Başta gerçek konuşma varsa (başkasıyla konuşma)
        bakılmaz, yani normal sohbet ek iş çıkarmaz."""
        head_s = getattr(gate, "HEAD_SECONDS", 2.5)
        start = step
        for _ in range(limit):
            if audio.seconds - start < 0.8:
                break
            part = Audio(audio.pcm[int(start * audio.rate) * 2:], audio.rate)
            text = gate.heard(part)
            if text and strip_wake(text) is not None:
                voice_log(f"uyandırma kaydın {start:.0f}. saniyesinde bulundu: {text}")
                return part, text
            if text and text.strip():
                break  # orada gerçek konuşma var ama uyandırma yok
            start += min(step, head_s)
        return audio, head

    def _full_text(self, audio: Audio) -> tuple[str, float] | None:
        """Kısa cümlede uyandırma kontrolü ve Live'a gidecek yazı için TEK yerel geçiş (iki ayrı geçiş ~0,5 sn
        kaybettiriyordu). Uzun cümlede ya da yazı yolu kapalıysa None: eski baş kontrolü yapılır."""
        gate = getattr(self.stt, "wake_gate", None)
        if (gate is None or not hasattr(gate, "transcribe") or self.live_text_min is None
                or audio.seconds > self.full_text_max):
            return None
        from alice.voice.vocab import whisper_prompt

        names = getattr(self.live, "names", None)
        t0 = time.monotonic()
        r = gate.transcribe(audio, whisper_prompt((names() if names else [])[:12]))
        if r is not None and hasattr(gate, "last_seconds"):
            gate.last_seconds = time.monotonic() - t0
        return r

    def _local_text(self, audio: Audio):
        """İlk cümleyi Live bağlanırken yerelde (GPU) ad ipuçlarıyla yazıya dök. Döküm güvenilirse Live'a ses
        yerine bu yazı gider. Dönen fonksiyon sonucu bekler (en çok live_text_wait sn)."""
        gate = getattr(self.stt, "wake_gate", None)
        if gate is None or not hasattr(gate, "transcribe") or self.live_text_min is None:
            return None
        box: dict[str, Any] = {}
        names = getattr(self.live, "names", None)
        pre, self._pre_text = self._pre_text, (None, False)
        follow = pre[1]

        def run() -> None:
            from alice.voice.vocab import whisper_prompt

            box["r"] = gate.transcribe(audio, whisper_prompt((names() if names else [])[:12]))

        if pre[0] is not None:
            box["r"] = pre[0]  # uyandırma kontrolünde zaten yazıya döküldü
            t = threading.Thread(target=lambda: None)
        else:
            t = threading.Thread(target=run, daemon=True, name="alice-live-text")
        t.start()

        def result() -> str | None:
            t.join(self.live_text_wait)
            r = box.get("r")
            if not r:
                voice_log("yerel döküm yetişmedi, ses gönderiliyor")
                return None
            text, conf = r
            rest = strip_wake(text)
            if rest is None and follow:
                rest = text.strip()
            words = [w.lower() for w in re.findall(r"\w+", rest or "")]
            looping = len(words) >= 3 and len(set(words)) <= len(words) // 2  # "YouTube, YouTube, YouTube"
            if not rest or looping or is_hallucination(rest) or conf < self.live_text_min:
                voice_log(f"yerel döküm güvenilmez ({conf:.2f}), ses gönderiliyor: {text}")
                return None
            return rest

        return result

    def _live_ready(self) -> bool:
        return self.live is not None and self.live.available and time.monotonic() > self._live_down

    def _converse_live(self, text: str, audio: Audio | None = None) -> bool:
        """Live oturumu aç ve sessizliğe kadar konuş. Açılamazsa False (eski yola dönülür)."""
        from alice.voice.live import LiveError

        if audio is None:
            self.speaker.wait(10)
        q: queue.Queue = self._live_q or queue.Queue()
        self._live_q = q
        try:
            if audio is not None:
                hint = self._local_text(audio)
                self.live.converse("", q, self.stop_event, first_audio=audio.resample(16000).pcm,
                                   **({"hint": hint} if hint else {}))
            else:
                self.live.converse(text, q, self.stop_event)
            return True
        except LiveError as e:
            voice_log(f"live açılamadı, eski yola dönülüyor: {e}")
            if e.limited:
                # Kota dolu: her 5 dakikada aynı İngilizce hatayı göstermek yerine bir saat normal sesli yol.
                self._live_down = time.monotonic() + LIVE_LIMIT_WAIT
                self.emit("Gemini canlı ses kotası doldu; bir saat normal sesli yolla (STT + model + TTS) devam ediyorum.")
            else:
                self._live_down = time.monotonic() + 300  # 5 dk eski yolla devam
                self.emit(f"live açılamadı: {e}")
            return False
        finally:
            self._live_q = None
            if getattr(self.live, "hushed", False) is True:
                self._hush("canlı konuşmada")
            else:
                # Pencere son konuşmadan (Alice'in ya da kullanıcının) itibaren sayılır; Live zaten o kadar sessizlikten
                # sonra kapandıysa ek süre kalmaz, yeniden "Alice" gerekir.
                last = getattr(self.live, "last_active", None)
                base = last if isinstance(last, (int, float)) and last > 0 else time.monotonic()
                self.armed_until = base + self.live_follow

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                self._handle_job(job)
            except Exception as e:  # noqa: BLE001 - tek bir iş (STT, CUDA, ağ) sesli asistanı kalıcı susturmasın
                self._busy.clear()
                voice_log(f"iş hatası ({type(e).__name__}): {e}")
                self.emit(f"hata: {e}")

    def _handle_job(self, job) -> None:
        audio = job if isinstance(job, Audio) else None
        if audio is not None:
            # Live açılamazsa eski yol: önce yazıya çevir.
            if self._live_ready():
                self._busy.set()
                try:
                    if self._converse_live("", audio):
                        return
                except Exception as e:  # noqa: BLE001
                    voice_log(f"live hatası: {e}")
                finally:
                    self._busy.clear()
            try:
                text = strip_wake(self.stt.transcribe(audio)) or ""
            except STTError as e:
                self.emit(f"anlaşılamadı: {e}")
                return
            if not text:
                return
        else:
            text = job
        if audio is None and self._live_ready():
            self._busy.set()
            try:
                if self._converse_live(text):
                    return
            except Exception as e:  # noqa: BLE001
                voice_log(f"live hatası: {e}")
            finally:
                self._busy.clear()
        if not text:
            return
        self._busy.set()
        now = time.monotonic()
        msgs = getattr(self.agent, "messages", None)
        if isinstance(msgs, list) and msgs:
            if self._last_turn and now - self._last_turn > self.idle_reset:
                msgs[:] = msgs[:1]  # uzun sessizlik: yeni sohbet
            else:
                msgs[:] = trim_history(msgs, self.keep_turns)
        reply = ""
        try:
            self.ui.begin_turn()
            reply = self.agent.run_turn(with_clock(text)) or ""
        except Exception as e:
            self.speaker.say("Bir sorun çıktı, ekrana yazdım.", "serious")
            self.emit(f"hata: {e}")
            reply = f"HATA {e}"
        finally:
            self._busy.clear()
            self._last_turn = time.monotonic()
            tools = f" araçlar: {', '.join(self.ui.tools)}" if self.ui.tools else ""
            voice_log(f"cevap [{', '.join(dict.fromkeys(self.ui.models))}]{tools}: {reply}")
        self.speaker.wait(120)
        if not self.muted:
            self.armed_until = time.monotonic() + self.follow_up

    # ------------------------------------------------------------------ döngü
    def run(self) -> None:
        self.ui.ask = self.ask_yes_no
        ptt = PushToTalk(self.ptt_key) if self.ptt_key else None
        wake = WakeWord(self.wake_model) if self.wake_model else None
        self.wake_phrase = self.wake_phrase and wake is None
        with self.mic_factory() as mic:
            mic_down: list[float] = []

            def frames():
                while not self.stop_event.is_set():
                    if ptt is not None and ptt.pressed.is_set():
                        self._interrupt()
                        audio = ptt.record(mic)
                        threading.Thread(target=self.handle_audio, args=(audio, True), daemon=True).start()
                        continue
                    try:
                        frame = mic.read()
                    except Exception as e:  # noqa: BLE001 - kulaklık çıkarıldı/kapandı: dinlemeyi öldürme, yeniden dene
                        if not mic_down:
                            voice_log(f"mikrofon okunamadı, yeniden deneniyor: {e}")
                            self.emit("mikrofon okunamıyor; tekrar deniyorum")
                        mic_down.append(time.monotonic())
                        self.stop_event.wait(1.0)
                        if hasattr(mic, "reopen"):
                            try:
                                mic.reopen()
                                voice_log(f"mikrofon yeniden açıldı: {getattr(mic, 'name', '')}")
                                mic_down.clear()
                            except Exception:  # noqa: BLE001
                                pass
                        continue
                    if self._mic_health(frame) and hasattr(mic, "reopen"):
                        try:
                            mic.reopen()
                            voice_log(f"mikrofon yeniden açıldı: {getattr(mic, 'name', '')}")
                        except Exception as e:  # noqa: BLE001
                            voice_log(f"mikrofon yeniden açılamadı: {e}")
                    if self._live_q is not None:  # canlı konuşmada mikrofon (gürültü süzülerek) Live'a
                        for f in (self.live_gate.feed(frame) if self.live_gate is not None else [frame]):
                            self._live_q.put(f)
                        continue
                    if wake is not None and wake.detect(frame):
                        self._interrupt()
                        self.muted = False
                        self.armed_until = time.monotonic() + self.follow_up
                        self.emit("uyandım")
                    # ALICE konuşurken kendi sesini konuşma sanmasın.
                    self.vad.boost = 2.5 if self.speaker.speaking else 1.0
                    yield frame

            gate = getattr(self.stt, "wake_gate", None)
            if gate is not None and hasattr(gate, "warm_up"):
                gate.warm_up()
            for audio in utterances(frames(), self.vad, stop=self.stop_event.is_set, end_silence=self.end_silence,
                                    on_start=self._on_speech_start):
                started = self._speech_at or None
                direct = self._armed(started)
                threading.Thread(target=self.handle_audio, args=(audio, direct, started), daemon=True).start()
        if ptt is not None:
            ptt.stop()

    def _mic_health(self, frame: bytes) -> bool:
        """Mikrofon tamamen sessizse (sessize alınmış, kapalı kulaklık) ALICE hiçbir şey duymaz ve kimse nedenini
        bilmez. 15 sn boyunca hiç ses yoksa bir kez söyle ve kaydet; ses geri gelince de kaydet."""
        """Döner: mikrofon akışı yeniden açılmalı mı (dijital sessizlik sürüyor; her 15 sn'de bir dener)."""
        now = time.monotonic()
        if not hasattr(self, "_mic_silent_since"):
            self._mic_silent_since, self._mic_warned, self._mic_retry = now, False, now
        if any(frame):  # tek bir sıfırdan farklı örnek bile yeter: dijital sessizlik değil
            if self._mic_warned:
                voice_log("mikrofondan ses geliyor yine")
            self._mic_silent_since, self._mic_warned, self._mic_retry = now, False, now
            return False
        if not self._mic_warned and now - self._mic_silent_since > 15:
            self._mic_warned = True
            voice_log("mikrofon tamamen sessiz: sessize alınmış ya da kapalı olabilir")
            self.emit("mikrofon sessiz")
            self.speaker.say("Mikrofonundan hiç ses gelmiyor, sessize alınmış olabilir.", "serious")
        if now - self._mic_retry > 15:
            self._mic_retry = now
            return True
        return False

    def stop(self) -> None:
        self.stop_event.set()
        self.speaker.stop()


def live_text_options(v: dict[str, Any]) -> dict[str, Any]:
    """[voice] live_text_min: ilk cümlenin yerel dökümüne güven eşiği (boş/false = hep ses gönder)."""
    m = v.get("live_text_min", -0.7)
    return {"live_text_min": None if m in ("", False, None) else float(m),
            "live_text_wait": float(v.get("live_text_wait", 1.5)),
            "live_follow": float(v.get("live_follow", FOLLOW_MAX)),
            "live_gate": bool(v.get("live_gate", True))}


def make_live(cfg, agent):
    """[voice] mode = "live" ise Gemini Live oturumu (anahtar ve websockets varsa)."""
    if str(cfg.data.get("voice", {}).get("mode", "live")) != "live":
        return None
    from alice.voice.live import LiveSession

    live = LiveSession(cfg, agent, log=voice_log)
    return live if live.available else None


def build_voice_agent(cfg, inner_ui=None, speaker: Speaker | None = None, cwd=None):
    """Sesli konuşma için ajan: normal araçlar + ses talimatı."""
    from pathlib import Path

    from alice.agent import Agent
    from alice.decider import Decider
    from alice.permissions import Permissions
    from alice.providers.router import Router
    from alice.tools import ToolContext, all_tools
    from alice.voice.tts import make_tts

    router = Router(cfg)
    decider = Decider.from_config(cfg, router)
    ctx = ToolContext(cwd=cwd or Path.home(), config=cfg, router=router, decider=decider)
    if speaker is None:
        tts = make_tts(cfg)
        tts.warm_up()
        speaker = Speaker(tts)
    ui = VoiceUI(speaker, inner_ui)
    agent = Agent(cfg, router, all_tools(cfg, ctx.emit), ctx, Permissions(cfg.agent.get("permission_mode", "auto")), ui, decider)
    agent.messages[0]["content"] += "\n\n" + VOICE_PROMPT
    from alice.memory import attach_memory

    attach_memory(agent, "ses")
    if "voice" in cfg.roles:
        agent.role = "voice"
    return agent, speaker, ctx
