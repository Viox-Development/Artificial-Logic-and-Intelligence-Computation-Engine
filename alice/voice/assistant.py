"""Voice assistant loop: listen -> transcribe -> ALICE -> speak.

- Push-to-talk always works; "Hey Alice" needs no hands.
- After ALICE answers, the conversation can continue for a few seconds without "Hey Alice".
- If you talk over ALICE while it speaks, it goes quiet, stops the unfinished work and listens to you.
- While streaming, the first sentence is spoken as soon as it is ready; before a tool runs it says "hemen bakıyorum".
"""

from __future__ import annotations

import queue
import random
import re
import threading
import time
from typing import Any, Callable

from alice.i18n import _, speech_language, speech_language_name
from alice.tools.base import ToolResult
from alice.voice.audio import Audio, Microphone
from alice.voice.lang import correction
from alice.voice.listen import (FOLLOW_MAX, PushToTalk, WakeWord, is_hush, is_yes, make_vad, strip_wake,
                               utterances)
from alice.voice.speaker import Speaker
from alice.voice.stt import STTError, is_hallucination
from alice.voice.tone import SentenceSplitter

VOICE_PROMPT = """# Voice mode
You are speaking out loud through text-to-speech. The user hears you, they may not be looking at the screen.
- Answer in {language}, in 1-3 short spoken sentences unless asked for detail. No markdown, lists, tables or emojis.
- Each user message ends with the current date and time in brackets. Use it for time/date questions (if it has a
  "spoken" form, say the time exactly that way);
  answer simple questions (time, date, small talk, general knowledge) directly without calling tools.
- Long-term memory is already in your system prompt: do not call the memory tool just to read it. When the user tells
  you something lasting about themselves (likes, dislikes, people, habits, how you should behave) save it with
  memory action=add; for "forget that" use action=forget; for past conversations not in your prompt use action=search.
- Weather: call the weather tool once (never run_command/curl for it).
- Never read code, paths or long numbers aloud; say you put it on screen.
- In urgent situations (flight warnings, errors) be clear and brief, no jokes.
- Talk like a friend with your own character (see "Your personality"), informal "sen", never "siz"/"efendim". Never
  use customer-service filler ("size nasıl yardımcı olabilirim", "başka bir konuda yardımcı olabilir miyim",
  "buradayım", "rica ederim, her zaman"). Do not offer help unprompted and never repeat a sentence you already said.
- Notice how the user feels from their words (frustrated, swearing, tired, excited) and react like a person would,
  on their side; no lecturing ("sakin ol").
- If the message was clearly not meant for you (talking to friends, game/video audio, another language), reply with
  an empty answer.
- Checklists (checklist tool): read only the current step, then wait; on "tamam", "devam" or "sonraki" call next.
- "Not al" during a flight goes to the kneeboard (kneeboard tool, action note); otherwise to the notes tool."""

# Questions that need thought go to the brain model, not the voice path's fastest model (first word soonest).
# Commands ("YouTube'u aç", "sesi kıs") stay on the fast path.
_THINK_RX = re.compile(
    r"\b(neden|niye|niçin|nasıl (çalışır|yapılır|yaparım|olur|yani)|açıkla|anlat|karşılaştır|farkı|hangisi (daha|iyi)|"
    r"öner|tavsiye|fikrin|ne düşünüyorsun|mantıklı mı|hesapla|kaç (para|lira|dolar|gün|saat|yıl)|plan(la|ı)?|"
    r"strateji|analiz|araştır|özetle|kod|script|algoritma|hata(yı|sı)? (neden|nasıl)|optimize|tasarla|"
    r"ne zaman|kimdir|nedir|ne demek|ne işe yarar|why|how (do|does|to|can)|explain|compare)\b", re.I)
_COMMAND_RX = re.compile(r"\b(aç|kapat|çal|durdur|duraklat|geç|sesi|kıs|yükselt|ekran görüntüsü|kilitle|hatırlat)\w*\b",
                         re.I)


def needs_thinking(text: str) -> bool:
    """Should this voice request go to the fast model (False) or the brain model (True)?"""
    t = " ".join((text or "").split())
    if not t:
        return False
    words = len(t.split())
    if _THINK_RX.search(t):
        return True
    return words >= 16 and not _COMMAND_RX.search(t)


def with_clock(text: str) -> str:
    """The voice assistant stays open for hours and the clock in the system prompt goes stale. Add the time and date
    to every utterance so questions like "saat kaç" are answered without running a command."""
    from datetime import datetime

    from alice.now import voice_line

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    now = datetime.now()
    state = voice_line()  # mode, game, flights, pulse: so "nasılım?" is answered in context
    # The Turkish spoken form of the time helps the voice say it naturally; English models say times well on their own.
    spoken = f", spoken: {spoken_time(now.hour, now.minute)}" if speech_language() == "tr" else ""
    return (f"{text}\n\n[now: {now:%d.%m.%Y} {days[now.weekday()]}, {now:%H:%M}{spoken}"
            + (f"; status: {state}" if state else "") + "]")


def voice_prompt() -> str:
    """VOICE_PROMPT for the current speech language."""
    return VOICE_PROMPT.replace("{language}", speech_language_name())


NUMS = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz", "on", "on bir", "on iki"]
TENS = {10: "on", 20: "yirmi"}
ACC = {"bir": "biri", "iki": "ikiyi", "üç": "üçü", "dört": "dördü", "beş": "beşi", "altı": "altıyı", "yedi": "yediyi",
       "sekiz": "sekizi", "dokuz": "dokuzu", "on": "onu"}
DAT = {"bir": "bire", "iki": "ikiye", "üç": "üçe", "dört": "dörde", "beş": "beşe", "altı": "altıya", "yedi": "yediye",
       "sekiz": "sekize", "dokuz": "dokuza", "on": "ona"}


def _num(n: int) -> str:
    return NUMS[n] if n <= 12 else " ".join(w for w in (TENS[n // 10 * 10], NUMS[n % 10]) if w)


def _case(word: str, table: dict[str, str]) -> str:
    head, _sep, last = word.rpartition(" ")
    return f"{head} {table[last]}".strip()


def spoken_time(hour: int, minute: int) -> str:
    """The model read "01:13" as "bire on üç" (one thirteen); hand it the Turkish spoken form of the time."""
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


# Spoken while a tool runs, per speech language.
FILLERS = {"tr": ["Hemen bakıyorum.", "Bir saniye, bakıyorum.", "Tamam, bakıyorum."],
           "en": ["Checking now.", "One second, let me look.", "Okay, looking."]}
LIVE_LIMIT_WAIT = 3600.0  # How long to stay on the normal voice path when the Live quota runs out (sec)


class VoiceUI:
    """The agent's UI: forwards text to the inner UI (terminal/app) and also speaks it."""

    def __init__(self, speaker: Speaker, inner=None, ask: Callable[[str], bool] | None = None):
        self.speaker = speaker
        self.inner = inner
        self.ask = ask
        self.splitter = SentenceSplitter()
        self.spoke = False
        self.muted = False  # interrupted: the rest of this turn is not spoken
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
            # No stream happened (stream off or fallback model): speak the whole text.
            rest = SentenceSplitter().feed(text + "\n") or [text]
        for s in rest:
            self._say(s)
        self.splitter = SentenceSplitter()
        if text and not self.muted:
            from alice.hud import views

            views.speech(text)  # if HUD is open, also show the list and counts in the corner

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self._inner("tool_start", call_id, name, summary)
        self.tools.append(f"{name}({summary[:80]})")
        if not self.spoke and not self.speaker.speaking:
            self._say(random.choice(FILLERS.get(speech_language(), FILLERS["en"])), "calm")

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        self._inner("tool_end", call_id, name, result)

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        if self.inner is not None and self.ask is None:
            return self.inner.ask_permission(name, summary, risk)
        if self.ask is None:
            return "n"
        return "y" if self.ask(_("Do you approve this: {summary}?", summary=summary[:120])) else "n"

    def info(self, msg: str) -> None:
        self._inner("info", msg)


# A short reaction to a bare "Alice". The same "Efendim?" every time sounded robotic: each one differs,
# and recently said ones are not repeated.
ACKS = ("Evet?", "Söyle.", "Hı?", "Dinliyorum.", "Evet, söyle.", "Hmm?", "Buyur.", "Ne var?")
ACKS_EN = ("Yes?", "Go ahead.", "Hm?", "I'm listening.", "Yes, go on.", "Hmm?", "What's up?")


def pick_ack(recent: list[str]) -> str:
    """Pick a reaction different from the last few and add it to the list."""
    acks = ACKS if speech_language() == "tr" else ACKS_EN
    choices = [a for a in acks if a not in recent[-3:]] or list(acks)
    ack = random.choice(choices)
    recent.append(ack)
    del recent[:-5]
    return ack


def voice_log(line: str) -> None:
    """~/.alice/voice.log: what was heard and what was answered (to spot misunderstandings)."""
    from datetime import datetime

    from alice.config import alice_home

    try:
        p = alice_home() / "voice.log"
        if p.exists() and p.stat().st_size > 2_000_000:
            p.replace(p.with_suffix(".log.1"))
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass  # logging must not break the assistant


def trim_history(messages: list[dict[str, Any]], keep_turns: int) -> list[dict[str, Any]]:
    """System prompt + the last keep_turns user turns (tool calls kept whole). The voice assistant stays open for hours;
    if the whole history went with every request it would drift to old topics and not fit small fast models."""
    starts = [i for i, m in enumerate(messages) if i and m.get("role") == "user" and isinstance(m.get("content"), str)
              and not m.get("_image")]
    if len(starts) <= keep_turns:
        return messages
    return messages[:1] + messages[starts[-keep_turns]:]


def _wake_like(text: str) -> bool:
    """Does the first word sound a bit like "Alice" ("Ayşe nabe", "Alessandra", "Elis")? Only these go to the
    second, paid ear; ordinary sentences said to a friend don't."""
    import difflib

    words = re.findall(r"\w+", text.lower())[:2]
    for w in words:
        if w in ("hey", "hay", "ey"):
            continue
        return w[:2] in ("al", "el", "ay", "ey", "ai", "ei") or difflib.SequenceMatcher(None, w[:5], "alice").ratio() >= 0.5
    return False


class VoiceAssistant:
    def __init__(self, agent, speaker: Speaker, stt, mic_factory: Callable[[], Microphone] = Microphone,
                 ptt_key: str = "", wake_model: str = "", wake_phrase: bool = True, barge_in: bool = True,
                 follow_up: float = FOLLOW_MAX, emit: Callable[[str], None] = lambda s: None,
                 keep_turns: int = 4, idle_reset: float = 300.0, live=None, end_silence: float = 0.5,
                 live_text_min: float | None = -0.7, live_text_wait: float = 1.5, live_follow: float = FOLLOW_MAX,
                 full_text_max: float = 8.0, live_gate: bool = True, smart_route: bool = True, speaker_gate=None,
                 social: Callable[[], str] | None = None, social_ptt: bool = False, social_follow: bool = True):
        self.agent = agent
        # Follow-up without "Alice" after replies on Discord/in a game (with a voice print, only the user's voice).
        self.social_follow = social_follow
        # The user's voice print (alice voice enroll): if present, only their voice wakes it (see speaker_id.py).
        self.speaker_gate = speaker_gate
        # Selective wake (on Discord or game open): returns the reason; empty means normal mode (see social.py).
        self.social = social or (lambda: "")
        # social_ptt (social_mode = "ptt"): on Discord/in a game the mic never reaches ALICE, only push-to-talk.
        # Off by default: the voice print already filters every sentence, and "Alice naber" also went unanswered.
        self.social_ptt = social_ptt and bool(ptt_key)
        self.keep_recent = False  # the core turns this on (so tests don't write to disk)
        self._social_noted = ""
        self._acks: list[str] = []  # recent short reactions ("Evet?", "Söyle."); no back-to-back repeats
        self.smart_route = smart_route  # send questions needing thought to the brain model, not the fast voice model
        # If the first sentence's local transcript is above this confidence (mean log-prob), Live gets text, not audio.
        self.live_text_min = live_text_min
        self.live_text_wait = live_text_wait
        self.live_follow = min(live_follow, FOLLOW_MAX)  # follow-up time without "Alice" after live talk ends
        self.full_text_max = full_text_max  # sentences up to this length are transcribed in one go on wake-up
        self._pre_text: tuple[Any, bool] = (None, False)
        from alice.voice.listen import SpeechGate

        self.live_gate: SpeechGate | None = SpeechGate() if live_gate else None
        self.end_silence = end_silence  # silence needed to decide that speech has ended (sec)
        self.live = live  # Gemini Live: speech-to-speech live talk after "Hey Alice"
        if live is not None:
            live.gated = live_gate  # frames sent to Live are gated: only those with real sound get through
            if hasattr(live, "barge_ok"):
                # While the live model speaks, a frame can barge in only if it passed voice-print verification.
                live.barge_ok = lambda: self.live_gate is not None and self.live_gate.verify is not None
        self._live_q: queue.Queue | None = None  # while set, mic frames go to Live
        self._live_down = 0.0
        self.keep_turns = keep_turns
        self.idle_reset = idle_reset  # new conversation after this many seconds of silence
        self._last_turn = 0.0
        self.speaker = speaker
        self.stt = stt
        self.mic_factory = mic_factory
        self.ptt_key = ptt_key
        self.wake_model = wake_model
        self.wake_phrase = wake_phrase
        self.barge_in = barge_in
        self.follow_up = min(follow_up, FOLLOW_MAX)
        self.muted = False  # "Alice sus" (silenced): nothing is a command until "Alice" is said again
        self._speech_at = 0.0  # when the last speech STARTED (the follow-up window is based on this)
        self.emit = emit
        self.ui: VoiceUI = agent.ui if isinstance(agent.ui, VoiceUI) else VoiceUI(speaker, agent.ui)
        agent.ui = self.ui
        self.vad = make_vad()  # Silero: clicks, breaths and game sound must not count as speech
        self.stop_event = threading.Event()
        self.armed_until = 0.0      # until then, no "Hey Alice" is needed
        self._jobs: queue.Queue = queue.Queue()
        self._answers: queue.Queue = queue.Queue()
        self._awaiting_answer = False
        self._cut: dict[str, Any] | None = None  # unsaid items when cut in: added to the answer as a note
        self._busy = threading.Event()
        threading.Thread(target=self._worker, daemon=True, name="alice-voice-turns").start()
        # One sentence at a time: a thread per sentence piled up on the Whisper lock, delaying replies up to 80 sec
        # in a game (1010 sec with the small model), and errors were written nowhere.
        self._utter_q: queue.Queue = queue.Queue()
        threading.Thread(target=self._utter_worker, daemon=True, name="alice-voice-utter").start()
        self._live_q_at = 0.0
        self._score: tuple[Audio, float] | None = None  # (audio, similarity): so the same audio isn't scored twice
        if live is not None:
            live.announce = self._announce  # result of a background job that finishes after the talk closes
            live.on_correction = self._corrected
        self.decision: dict[str, Any] = {}
        self._last_wake_at = 0.0
        # Each sentence's decision (audio + verdict): the core links it to a voice corpus (alice/voice/corpus.py)
        self.on_decision: Callable[[Audio, dict[str, Any], Any], None] | None = None
        self.on_correct: Callable[[str], None] | None = None

    # ---------------------------------------------------------------- events
    def _interrupt(self) -> None:
        if self.speaker.speaking or self._busy.is_set():
            if self._awaiting_answer:
                self.speaker.stop()
                return
            self.ui.mute()
            self._cut = getattr(self.speaker, "last_cut", None)
            if self._busy.is_set():
                self.agent.stop()
            self.emit("araya girildi, susuyorum")

    def _on_speech_start(self) -> None:
        self._speech_at = time.monotonic()
        if self.speaker.speaking and not self.muted and not getattr(self.speaker, "alerting", False) is True:
            # Cutting in while Alice talks is also said to her. A game/flight alert ("Kilitlendin!") while speaking
            # not: a sentence said to a friend right after the alert went to the live model, and ALICE
            # started talking for no reason (Nov 7).
            self.armed_until = max(self.armed_until, self._speech_at)
        self._barge = self.barge_in and self.speaker.speaking and not self.social()
        if self._barge and not self._gate_ready():
            # No voice print: the old behavior, stop as soon as speech starts. If there is one, handle_audio cuts
            # only once it hears the sound is the user's; clicks, breaths, or a friend cutting ALICE off must not.
            self._interrupt()

    def _armed(self, at: float | None = None) -> bool:
        """Can speech be taken without 'Alice'? The moment the speech started is checked: a sentence that starts
        inside the window and ends outside it counts; one that starts after the window has closed does not."""
        if self.muted:
            return False
        if self.social():
            # A sentence said to a friend on Discord or in a game must not reach ALICE. Exceptions: if ALICE just
            # answered "Evet, söyle" ("Yes, go ahead") to a bare "Alice", the sentence after it was meant for it
            # ("Alice" ... "naber" ("what's up") went unanswered, Oct 30); if a voice print is on file, the follow-up
            # window after an answer is also open, and the sentence is still checked to be the user's (before, the
            # conversation closed after every answer, so an answer given without "Alice" was not heard, Nov 6).
            if (at or time.monotonic()) <= getattr(self, "_called_until", 0.0):
                return True
            # Only on the ChatGPT Realtime path: the user talks to friends too ("Ahmet gel buraya" = "come here"),
            # and a voice print can't tell these apart. The live model hears it and stays silent unless addressed.
            # On the old path (transcribe, then answer) there is no such filter, so Discord still requires "Alice".
            return bool(getattr(self, "social_follow", True)) and self._gate_ready() and self._live_ready() \
                and getattr(self.live, "prefers_audio", False) is True and (at or time.monotonic()) <= self.armed_until
        return (at or time.monotonic()) <= self.armed_until

    def _strict(self) -> bool:
        """Must 'Alice' be at the start of the sentence? Yes, when muted or in selective mode."""
        return self.muted or bool(self.social())

    def _hush(self, why: str = "") -> None:
        """'Alice sus' ("Be quiet"): stop talking, close the follow-up window, listen again only after 'Alice'."""
        self.muted = True
        self.armed_until = 0.0
        self.speaker.stop()
        if self._busy.is_set():
            self.ui.mute()
            self.agent.stop()
        self._hud(None)
        voice_log(f"susturuldu{f': {why}' if why else ''} (yeniden 'Alice' denene kadar komut alınmıyor)")
        self.emit("sustum; 'Alice' diyene kadar dinlemiyorum")

    def ask_yes_no(self, question: str, timeout: float = 10) -> bool:
        """Voice confirmation: say the question, and count the next utterance as the answer."""
        while not self._answers.empty():  # an answer left over from the previous question must not confirm this one
            try:
                self._answers.get_nowait()
            except queue.Empty:
                break
        # The answer wait opens before the question is spoken: saying "evet" ("yes") while the question was still
        # playing used to count as a new request, and it stopped the turn that asked the question.
        self._awaiting_answer = True
        self.speaker.say(question, "serious")
        self.speaker.wait(15)
        self.armed_until = time.monotonic() + timeout
        try:
            ans = self._answers.get(timeout=timeout)
        except queue.Empty:
            return False
        finally:
            self._awaiting_answer = False
        return is_yes(ans)

    def handle_text(self, text: str, direct: bool) -> None:
        """Process transcribed speech (the testable core)."""
        text = text.strip()
        if not text:
            return
        rest = strip_wake(text, strict=self._strict())
        armed = direct or self._armed()
        if is_hush(text) and (armed or rest is not None or self._awaiting_answer or self.speaker.speaking
                              or self._busy.is_set()):
            if self._awaiting_answer:
                self._answers.put("hayır")
            self._hush(text)
            return self._decide("susturma", woke=True)
        if self._awaiting_answer:
            self._answers.put(text)
            return self._decide("cevap", woke=True)
        if self.muted:
            if rest is None and not direct:
                return self._decide("susturulmus")  # muted: only "Alice ..." or push-to-talk wakes it
            self.muted = False
            voice_log("susturma kalktı")
        # Strip the wake word whenever present; otherwise it only applies to push-to-talk or the follow-up window.
        if rest is not None and (armed or self.wake_phrase):
            text = rest
        elif not armed:
            return self._decide("uyandirma_yok")
        if correction(text):
            self._corrected(text)  # a correction said to ALICE (classic path; in Live, LiveSession.on_correction)
        self._decide("uyandi" if rest is not None else "takip", woke=True)
        if not text:
            self.speaker.say(pick_ack(self._acks), "calm")
            if self._live_ready():
                self._jobs.put("")  # open the live conversation; the user will speak
                return
            self.speaker.wait(5)  # start the window after the short reaction finishes
            self.armed_until = self._called_until = time.monotonic() + self.follow_up
            return
        if self._busy.is_set():
            self.ui.mute()
            self.agent.stop()
        self._jobs.put(text)

    def _second_ear(self, audio: Audio, head: str = "") -> str:
        """If the wake word was missed: Groq first (free, ~0.5 s), then ElevenLabs Scribe if needed ("" = not found)."""
        def ask(ear) -> str:
            try:
                return ear.transcribe(audio) if ear is not None else ""
            except Exception:  # noqa: BLE001 - if the second ear fails, the earlier decision stands
                return ""

        engines = getattr(self.stt, "engines", [])
        groq = next((e for e in engines if str(getattr(e, "name", "")).startswith("groq:")), None)
        text = ask(groq)
        if text and strip_wake(text, strict=self._strict()) is not None:
            return text
        scribe = getattr(self.stt, "second_ear", None) or next(
            (e for e in engines if str(getattr(e, "name", "")).startswith("eleven:")), None)
        if scribe is not None and (_wake_like(text) or _wake_like(head)):
            return ask(scribe)
        return text

    JOIN_MAX = 2.0  # sec: an unwoken sentence this short can be joined with the next one ("Alice" ... "naber")
    JOIN_GAP = 2.5  # sec: a sentence arriving this long after the previous one ends

    def _take_unwoken(self) -> Audio | None:
        """The short sentence that just arrived without a wake word (handed out only once)."""
        prev, self._unwoken = getattr(self, "_unwoken", None), None
        if prev is None or time.monotonic() - prev[1] > self.JOIN_GAP + self.JOIN_MAX + 1.0:
            return None
        return prev[0]

    def _gate_ready(self) -> bool:
        gate = self.speaker_gate
        return bool(gate is not None and getattr(gate, "active", False) and getattr(gate, "usable", lambda: True)())

    def _voice_first(self, audio: Audio, started: float | None) -> bool:
        """Every sentence from the open mic is compared with the voice print first (~20 ms, before transcription):
        another person's voice and noise are never processed. Very short sounds (clicks, breaths, under 0.6 s)
        are taken only while awaiting an answer ("evet") or in the follow-up window; Whisper made up "Altyazı M.K."."""
        if not self._gate_ready():
            return True
        from alice.voice.speaker_id import MIN_SECONDS

        if audio.seconds < MIN_SECONDS:
            # The voice print is unreliable on audio this short, so the decision is left to the wake-word check.
            # It used to be dropped early, which is why even a fast "Alice naber" (0.55 s) never got an answer.
            return True
        return self._is_user(audio, wake=True)  # the gray zone passes for now; the wake word is checked later

    RECENT_KEEP = 30

    def _keep_recent(self, audio: Audio):
        """Last 30 sentences' audio goes to ~/.alice/voice/recent, so "cevap vermedi" can be tested on it.
        Returns: the wav path (a decision file is written next to it), or None."""
        try:
            from alice.config import alice_home

            d = alice_home() / "voice" / "recent"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.monotonic() * 1000) % 1000:03d}.wav"
            audio.save(path)
            for old in sorted(d.glob("*.wav"))[:-self.RECENT_KEEP]:
                old.unlink()
                old.with_suffix(".json").unlink(missing_ok=True)
            return path
        except Exception:  # noqa: BLE001 - listening goes on even if the recording cannot be saved
            return None

    def _decide(self, reason: str, woke: bool = False, **extra: Any) -> None:
        """The decision for this sentence: the recording and corpus go to its side file; tests check real audio.
        reason: kisa | ses_izi | gri | susturma | uyandirma_yok | uyandi | takip | stt_hata | halusinasyon |
        susturulmus | cevap. The first decision made is the one that counts."""
        d = getattr(self, "decision", None)
        if isinstance(d, dict) and not d.get("final"):
            d.update(extra, reason=reason, woke=woke, final=True)
            if woke:
                self._last_wake_at = time.monotonic()

    def handle_audio(self, audio: Audio, direct: bool, started: float | None = None, ptt: bool = False) -> None:
        self.decision: dict[str, Any] = {"reason": "?", "woke": False, "direct": direct, "ptt": ptt,
                                         "armed": self._armed(started), "social": self.social() or "",
                                         "muted": self.muted, "awaiting": self._awaiting_answer}
        path = self._keep_recent(audio) if self.keep_recent and audio.seconds >= 0.3 else None
        self._final_audio = None
        try:
            self._handle_audio(audio, direct, started, ptt)
        finally:
            d = self.decision
            d.pop("final", None)
            final = self._final_audio or audio  # audio the decision was made on (may be joined with the previous)
            if final is not audio:
                d["joined"] = True
                if path is not None:
                    try:
                        final.save(path)
                    except Exception:  # noqa: BLE001
                        pass
            audio = final
            score = getattr(self, "_score", None)
            if score is not None and score[0] is audio:
                d["spk"] = round(score[1], 3)
            if self.speaker_gate is not None and getattr(self.speaker_gate, "active", False):
                d["thr"] = float(getattr(self.speaker_gate, "threshold", 0.0))
            d["dur"] = round(audio.seconds, 2)
            if path is not None:
                try:
                    import json

                    path.with_suffix(".json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
            hook = getattr(self, "on_decision", None)
            if hook is not None:
                try:
                    hook(audio, d, path)
                except Exception as e:  # noqa: BLE001 - a corpus error must not break listening
                    voice_log(f"korpus kaydı yazılamadı: {e}")

    def _handle_audio(self, audio: Audio, direct: bool, started: float | None, ptt: bool) -> None:
        if audio.seconds < 0.3:
            return self._decide("kisa")
        if not ptt and not self._voice_first(audio, started):
            return self._decide("ses_izi")
        if not ptt and getattr(self, "_barge", False) and self._gate_ready():
            self._barge = False
            if self._is_user(audio):  # gray-zone audio (likely echo or someone else) must not interrupt speech
                self._interrupt()  # the user's voice is verified: stop now
        if direct and not ptt and self.social() and not self._awaiting_answer \
                and (started or 0.0) > getattr(self, "_called_until", 0.0) and not self._is_user(audio):
            # Follow-up window on Discord: a friend's voice must not reach ALICE (the voice print did not verify it).
            voice_log("takip penceresinde başkası konuştu (ses izi), cevap yok")
            return self._decide("gri")
        if (direct or ptt) and self._live_ready() and not self._busy.is_set() \
                and getattr(self.live, "prefers_audio", False) is True and audio.seconds >= 0.3:
            # A sentence in the follow-up window (continuing without "Alice") or from push-to-talk: the live model
            # hears the audio itself. It used to be transcribed by Groq Whisper first, so the live model got misspelled
            # text, not the sound ("İyi geçti günüm" -> "İyi eğitim geçti gönöm"), and STT added ~1 s of delay.
            voice_log(f"takip: ses doğrudan canlı modele ({audio.seconds:.1f} sn ses, yazıya çevrilmedi)")
            self._live_q_at = time.monotonic()
            self._live_q = queue.Queue()
            self._pre_text = (None, True)
            self._final_audio = audio
            self.decision.update(path="live_audio")
            self._jobs.put(audio)
            return self._decide("takip", woke=True)
        gate = getattr(self.stt, "wake_gate", None)
        if not direct and not self._awaiting_answer and gate is not None and self.wake_phrase:
            armed = self._armed(started)  # it just answered: continue without "Alice"
            # Open the connection together with the wake check. In selective mode (Discord, games) nearly all sentences
            # are for friends, and each one opened a Live connection for nothing (95 wake-ups in 7,699 sentences).
            pre = (self._live_ready() and not self._busy.is_set() and audio.seconds >= 0.6
                   and (armed or not self.social()))
            owner = object()  # this sentence's connection: a sentence without a wake word must not close it
            if pre:
                self.live.prefetch(owner=owner)
            full = self._full_text(audio) if pre else None
            head = full[0] if full else gate.heard(audio)
            if head is not None and not head.strip() and not full:
                audio, head = self._scan_wake(gate, audio, head)
            conf = full[1] if full else getattr(gate, "last_conf", 0.0)
            # Continuing without "Alice": a transcript made up from noise, or a very unsure one, must not reopen Live.
            follow = (armed and len(head.strip()) > 1 and not is_hallucination(head) and conf > -1.2
                      if head is not None else False)
            woke = head is not None and (strip_wake(head, strict=self._strict()) is not None or follow)
            prev = self._take_unwoken() if head is not None and not woke else None
            if head is not None and not woke and audio.seconds <= 3.0 and (head.strip() or prev is not None):
                # The small local model can hear "Alice naber" as "Ya anlanan bir an mıydı?": on a short sentence, ask
                # free Groq first; if it hears something like "Alice" but is unsure, ask Scribe (paid). On short audio
                # the small model also makes up "Altyazı", so don't drop it as noise: send it to the second ear.
                # When "Alice ... naber" pauses into two sentences, the unwoken short sentence before it is joined and
                # heard ("Alice" alone was heard as "Ay" and thrown away, Oct 30).
                ear = prev + Audio.silence(0.15, audio.rate) + audio if prev is not None else audio
                better = self._second_ear(ear, head)
                if better and strip_wake(better, strict=self._strict()) is not None:
                    voice_log(f"uyandırmayı ikinci kulak buldu{' (önceki cümleyle)' if prev is not None else ''}: "
                              f"{head!r} -> {better!r}")
                    head, woke, full, audio = better, True, None, ear
            if pre and not woke:
                self.live.discard(owner=owner)
            if head is not None:
                self.decision.update(head=head, path="full" if full else "heard")
                self._final_audio = audio
            if head is not None and woke and is_hush(head):
                if pre:
                    self.live.discard(owner=owner)
                self._hush(head)  # "Alice sus" must not open a live conversation
                return self._decide("susturma", woke=True)
            if head is not None and woke and not self._is_user(audio, wake=strip_wake(head) is not None):
                if pre:
                    self.live.discard(owner=owner)
                return self._decide("gri")
            if head is not None and woke and self.muted:
                self.muted = False
                voice_log("susturma kalktı")
            if head is not None:
                if not woke:
                    if audio.seconds <= self.JOIN_MAX:
                        self._unwoken = (audio, time.monotonic())
                    if audio.seconds >= 0.6:
                        voice_log(f"uyandırma yok [{_gate_time(gate)}]: {head}")
                    return self._decide("uyandirma_yok")  # no wake word: never send the audio to the cloud
                direct = True  # the local model heard "Hey Alice"; accept it even if the cloud transcript differs
                if self._live_ready() and not self._busy.is_set():
                    # Live conversation: skip transcription, send the sentence's audio straight to Live (~0.5 s saved).
                    voice_log(f"uyandı [{_gate_time(gate)}]: {head}")
                    self._live_q_at = time.monotonic()
                    self._live_q = queue.Queue()  # so nothing said while connecting is lost: frames pile up now
                    self._pre_text = (full, armed and strip_wake(head) is None)
                    self._jobs.put(audio)
                    return self._decide("uyandi" if strip_wake(head) is not None else "takip", woke=True)
        try:
            text = self.stt.transcribe(audio)
        except STTError as e:
            self.emit(f"anlaşılamadı: {e}")
            return self._decide("stt_hata")
        self.emit(_("heard: {text}", text=text))
        self.decision.update(head=text, path="stt")
        if is_hallucination(text):
            voice_log(f"duyulan (gürültü sayıldı) [{getattr(self.stt, 'last', '')}]: {text}")
            return self._decide("halusinasyon")
        voice_log(f"duyulan [{getattr(self.stt, 'last', '')}]{' (doğrudan)' if direct else ''}: {text}")
        if (not direct and not self._awaiting_answer and not is_hush(text)
                and (strip_wake(text, strict=self._strict()) is not None or self._armed())
                and not self._is_user(audio, wake=strip_wake(text, strict=self._strict()) is not None)):
            return self._decide("gri")
        self.handle_text(text, direct)

    # Voice-print scores overlap between the user and others (the lowest 10% of accepts are at 0.38, the highest
    # reject at 0.42): anything this far below the threshold is the "gray zone". There, audio is accepted only if
    # "Alice" was clearly called out; a sentence without "Alice" in the follow-up window, and barge-in, are rejected.
    GRAY = 0.08

    def _is_user(self, audio: Audio, wake: bool = False) -> bool:
        """Is this the user's voice? No voice print: everyone is accepted. "Alice sus" and push-to-talk skip this.
        wake: the sentence contains a wake word (gray-zone audio is accepted too)."""
        from alice.voice.speaker_id import enrolling

        if enrolling():
            voice_log("uygulamada ses tanıtılıyor: okunan cümleye cevap yok")
            return False
        gate = self.speaker_gate
        if gate is None:
            return True
        thr = float(getattr(gate, "threshold", 0.0))
        cached = getattr(self, "_score", None)
        if cached is not None and cached[0] is audio:  # not id(): a freed Audio's id gets reused by the next one
            score = cached[1]  # this audio was just scored
        else:
            gate.check(audio.pcm, audio.rate)
            if not getattr(gate, "active", True):
                return True  # no voice enrolled yet: everyone is accepted, nothing is logged
            score = float(gate.last_score)
            self._score = (audio, score)
            note = getattr(gate, "last_note", "")
            if score >= thr:
                voice_log(f"ses tanındı ({score:.3f}{', ' + note if note else ''})")
            elif score >= thr - self.GRAY:
                voice_log(f"ses gri bölgede ({score:.3f}, eşik {thr:.3f}): sadece 'Alice' diye seslenince kabul")
            else:
                voice_log(f"başka biri konuştu (ses benzerliği {score:.3f} < {thr:.3f}), cevap yok")
        if score >= thr:
            return True
        return wake and score >= thr - self.GRAY

    def _scan_wake(self, gate, audio: Audio, head: str, step: float = 2.0, limit: int = 5) -> tuple[Audio, str]:
        """If the sentence does not start with speech (a long recording starting with game/music sound), "Hey Alice"
        may come later: look at the next windows and, if found, start the recording from there. If there is real
        speech at the start (talking to someone else), it does not look further, so normal chat costs no extra work."""
        head_s = getattr(gate, "HEAD_SECONDS", 2.5)
        start = step
        for _try in range(limit):
            if audio.seconds - start < 0.8:
                break
            part = Audio(audio.pcm[int(start * audio.rate) * 2:], audio.rate)
            text = gate.heard(part)
            if text and strip_wake(text) is not None:
                voice_log(f"uyandırma kaydın {start:.0f}. saniyesinde bulundu: {text}")
                return part, text
            if text and text.strip():
                break  # there is real speech there, but no wake word
            start += min(step, head_s)
        return audio, head

    def _full_text(self, audio: Audio) -> tuple[str, float] | None:
        """For a short sentence, ONE local pass for both the wake check and the text sent to Live (two separate passes
        cost ~0.5 s). For a long sentence, or with the text path off, None: the old head check is done."""
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
        """Transcribe the first sentence locally (GPU) with name hints while Live connects. If the transcript is
        reliable, this text goes to Live instead of the audio. The returned function waits for the result (at most
        live_text_wait s)."""
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
            box["r"] = pre[0]  # already transcribed during the wake check
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
        """Open a Live session and talk until silence. False if it cannot be opened (falls back to the old path)."""
        from alice.voice.live import LiveError

        if audio is None:
            self.speaker.wait(10)
        q: queue.Queue = self._live_q or queue.Queue()
        self._live_q = q
        if hasattr(self.live, "single_turn"):
            self.live.single_turn = bool(self.social())  # in Discord/a game: one answer, then close
        try:
            if audio is not None:
                social = bool(self.social())
                follow = self._pre_text[1]  # they said "Alice" and ALICE reacted: this sentence is not expected to contain "Alice"
                # In selective mode the first sentence always goes as audio: under a game the local transcript took seconds
                # (7.1 s measured), and when text was sent Gemini could not confirm "Alice" with its own ear.
                # ChatGPT Realtime hears the audio itself: don't wait for the local transcript (≤1.5 s) to send text.
                hint = None if social or getattr(self.live, "prefers_audio", False) is True else self._local_text(audio)
                self._pre_text = (None, False)
                extra: dict[str, Any] = {"hint": hint} if hint else {}
                if getattr(audio, "ended", None):
                    extra["ended"] = audio.ended  # end-to-end latency: the moment the user stopped talking
                if social and follow and getattr(self.live, "prefers_audio", False) is True:
                    extra["check_addressed"] = True  # Discord follow-up: let Jev decide "was this said to ALICE"
                if social and not follow:
                    extra["need_wake"] = True  # selective mode: Gemini must hear "Alice" too (see LiveSession)
                self.live.converse("", q, self.stop_event, first_audio=audio.resample(16000).pcm, **extra)
            else:
                self.live.converse(text, q, self.stop_event)
            return True
        except LiveError as e:
            voice_log(f"live açılamadı, eski yola dönülüyor: {e}")
            if e.limited:
                # Quota exhausted: instead of showing the same English error every 5 minutes, use the normal voice path for an hour.
                self._live_down = time.monotonic() + LIVE_LIMIT_WAIT
                self.emit(_("The Gemini live voice quota is used up; for an hour I'll carry on with the normal voice path (STT + model + TTS)."))
            else:
                self._live_down = time.monotonic() + 300  # 5 dk eski yolla devam
                self.emit(f"live açılamadı: {e}")
            return False
        finally:
            self._live_q = None
            if getattr(self.live, "hushed", False) is True:
                self._hush("canlı konuşmada")
            else:
                # The window counts from the last speech (Alice's or the user's); if Live already closed after that much
                # silence, no time is left and "Alice" is needed again.
                last = getattr(self.live, "last_active", None)
                base = last if isinstance(last, (int, float)) and last > 0 else time.monotonic()
                # A sentence said to a friend (Jev) must not open a new follow-up window.
                self.armed_until = 0.0 if getattr(self.live, "declined", False) is True else base + self.live_follow

    UTTER_MAX_AGE = 8.0  # s: an older sentence is skipped if a newer one is queued behind it; it also no longer counts as a follow-up without "Alice"
    UTTER_HARD_AGE = 20.0  # s: anything older is always skipped (no point answering anymore)

    def _utter_worker(self) -> None:
        while True:
            audio, direct, started, ptt, queued = self._utter_q.get()
            age = time.monotonic() - queued
            if not ptt and not self._awaiting_answer and age > self.UTTER_MAX_AGE:
                # In games, when Whisper slowed down, even a single queued sentence was dropped and the "Alice" in it was
                # never heard (59 sentences in two days, most under 20 s). If no newer one follows, it is processed, but
                # the follow-up window has expired: it is answered only if "Alice" was said explicitly.
                if age > self.UTTER_HARD_AGE or not self._utter_q.empty():
                    voice_log(f"eski cümle atlandı ({age:.1f} sn sırada bekledi, {audio.seconds:.1f} sn ses)")
                    continue
                direct, started = False, None
            try:
                self.handle_audio(audio, direct, started, ptt)
            except Exception as e:  # noqa: BLE001 - one sentence must not break listening, but leave a trace
                voice_log(f"cümle işlenemedi ({type(e).__name__}): {e}")

    def _announce(self, text: str) -> None:
        """Result of a job that finished after the live conversation closed: show it on the island and say it unless
        hushed (after "Alice sus" (Alice, be quiet) the result went to the closed connection and was lost)."""
        voice_log(f"arka plan sonucu konuşma dışında iletildi: {text[:200]}")
        try:
            from alice.hud import views

            views.speech(text)
        except Exception:  # noqa: BLE001
            pass
        if not self.muted:
            self.speaker.say(text, "calm")

    def _corrected(self, text: str) -> None:
        """The user corrected the last wake-up ("sana demedim" (I wasn't talking to you), "yanlış anladın" (you
        misunderstood)): the corpus labels that sentence."""
        if not self._last_wake_at or time.monotonic() - self._last_wake_at > 120:
            return
        hook = getattr(self, "on_correct", None)
        if hook is not None:
            try:
                hook(text)
            except Exception as e:  # noqa: BLE001
                voice_log(f"korpus düzeltmesi yazılamadı: {e}")

    def _hud(self, state: str | None, detail: str = "") -> None:
        """Voice state on the island (the classic path too: it used to be shown only for Live)."""
        try:
            from alice.hud import views

            views.voice_state(state, detail)
        except Exception:  # noqa: BLE001 - a HUD error must not break the conversation
            pass

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                self._handle_job(job)
            except Exception as e:  # noqa: BLE001 - one job (STT, CUDA, network) must not silence the voice assistant for good
                self._busy.clear()
                voice_log(f"iş hatası ({type(e).__name__}): {e}")
                self.emit(f"hata: {e}")

    LIVE_Q_ORPHAN = 20.0  # sn

    def _tune_live_gate(self) -> None:
        """While ALICE speaks in a live conversation, its own voice and noise must not interrupt it: the VAD threshold
        goes up (this used to exist only on the classic path; in ~21% of interruptions the heard text was empty). In
        Discord/games the interrupting voice is also verified to be the user's."""
        gate = self.live_gate
        playing = bool(getattr(self.live, "playing", False))
        gate.vad.boost = 2.5 if playing else 1.0
        # Is the voice cutting in while ALICE speaks the user's (voice print)? If verified, the live model goes quiet
        # (barge_ok). It used to be verified only in Discord/games, and the ChatGPT session dropped every frame
        # arriving while it spoke: the user could never interrupt ALICE (7 November). Echo (ALICE's own voice)
        # cannot pass the voice print.
        verify = playing and self._gate_ready() and (self.barge_in or bool(self.social()))
        gate.verify = (lambda pcm: self.speaker_gate.check(pcm, 16000)) if verify else None

    def _handle_job(self, job) -> None:
        started = time.monotonic()  # speech ended and the job is queued: latency is counted from here to the first sound
        audio = job if isinstance(job, Audio) else None
        if audio is not None:
            # If Live cannot be opened, the old path: transcribe first.
            if self._live_ready():
                self._busy.set()
                try:
                    if self._converse_live("", audio):
                        return
                except Exception as e:  # noqa: BLE001
                    voice_log(f"live hatası: {e}")
                finally:
                    self._busy.clear()
            # Only the live conversation drains the queue opened on wake-up. If Live went back to "not ready" meanwhile
            # (it just errored), the queue stayed open, every mic frame went there and ALICE heard nothing.
            self._live_q = None
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
        old_role = getattr(self.agent, "role", "brain")
        if self.smart_route and old_role == "voice" and needs_thinking(text):
            self.agent.role = "brain"  # for this turn: starts a bit later but gives a correct, complete answer
            voice_log("düşünmesi gereken soru: beyin modeline gidiyor")
        expect = getattr(self.speaker, "expect", None)
        if callable(expect):
            expect(started, lambda wait: self._latency(wait, audio is not None))
        try:
            self.ui.begin_turn()
            self._hud("think")
            reply = self.agent.run_turn(with_clock(text)) or ""
        except Exception as e:
            self.speaker.say(_("Something went wrong, I put it on the screen."), "serious")
            self.emit(f"hata: {e}")
            reply = f"HATA {e}"
        finally:
            if hasattr(self.agent, "role"):
                self.agent.role = old_role
            self._busy.clear()
            self._last_turn = time.monotonic()
            tools = f" tools: {', '.join(self.ui.tools)}" if self.ui.tools else ""
            voice_log(f"cevap [{', '.join(dict.fromkeys(self.ui.models))}]{tools}: {reply}")
        self._hud("speak")
        self.speaker.wait(120)
        self._hud(None)
        if callable(expect):
            expect(None)  # if the answer had no audio, don't measure the next notification by mistake
        self._note_cut()
        if not self.muted:
            self.armed_until = time.monotonic() + self.follow_up

    def _latency(self, wait: float, spoken: bool) -> None:
        from alice.voice import latency

        latency.record("klasik", wait, tools=bool(self.ui.tools), stt=spoken,
                       model=", ".join(dict.fromkeys(self.ui.models)))

    def _note_cut(self) -> None:
        """If ALICE was interrupted, note the unspoken part under the answer: on "devam et" (go on) the model continues
        from there instead of starting over (the model thought it had said the whole answer)."""
        cut, self._cut = self._cut, None
        msgs = getattr(self.agent, "messages", None)
        if not cut or not isinstance(msgs, list):
            return
        from alice.voice.bargein import cut_note

        playing = str(cut.get("playing") or "")
        unheard = " ".join(([f"{playing} (cut off in the middle of this sentence)"] if playing else [])
                           + [str(t) for t in cut.get("pending") or []])
        note = cut_note(None, unheard)
        last = next((m for m in reversed(msgs) if m.get("role") == "assistant" and isinstance(m.get("content"), str)),
                    None)
        if note and last is not None:
            last["content"] = last["content"].rstrip() + "\n\n" + note
            voice_log(f"söz kesildi, söylenemeyen kısım not edildi: {unheard[:120]}")

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        self.ui.ask = self.ask_yes_no
        ptt = PushToTalk(self.ptt_key) if self.ptt_key else None
        wake = WakeWord(self.wake_model) if self.wake_model else None
        self.wake_phrase = self.wake_phrase and wake is None
        try:
            opened = self._open_mic()
            if opened is not None:
                cm, mic = opened
                try:
                    while True:  # an unexpected error must not kill listening for good: log it, recover, continue
                        try:
                            self._listen(mic, ptt, wake)
                            break
                        except Exception as e:  # noqa: BLE001
                            if self.stop_event.is_set():
                                break
                            voice_log(f"dinleme döngüsü hata verdi, yeniden başlıyor: {e!r}")
                            time.sleep(1.0)
                finally:
                    cm.__exit__(None, None, None)
        finally:
            if ptt is not None:
                ptt.stop()

    def _open_mic(self, retry: float = 5.0):
        """Open the microphone. If the device is missing/busy, instead of dying with a stack trace (in the core the
        audio thread ended silently), give one clear message and retry at intervals; listening starts once it is
        plugged in. If the package (sounddevice) is missing there is no point retrying: None."""
        warned = False
        while not self.stop_event.is_set():
            try:
                cm = self.mic_factory()
                mic = cm.__enter__()
            except ImportError as e:
                voice_log(f"mikrofon kullanılamıyor: {e}")
                self.emit(_("the microphone can't be used ({name} is not installed): ", name=getattr(e, 'name', None) or e)
                          + "pip install 'alice-harness[voice]'")
                return None
            except Exception as e:  # noqa: BLE001 - PortAudioError, OSError: device missing/busy
                if not warned:
                    warned = True
                    voice_log(f"mikrofon açılamadı, {retry:.0f} sn'de bir yeniden denenecek: {e}")
                    self.emit(_("could not open the microphone: {error}; I'll start listening when it is plugged in", error=e))
                self.stop_event.wait(retry)
                continue
            if warned:
                voice_log(f"mikrofon açıldı: {getattr(mic, 'name', '')}")
            return cm, mic
        return None

    def _listen(self, mic, ptt, wake) -> None:
        mic_down: list[float] = []

        def mic_failed(e: Exception) -> None:
            """The headset was unplugged/turned off: don't kill listening, wait and try to reopen."""
            if not mic_down:
                voice_log(f"mikrofon okunamadı, yeniden deneniyor: {e}")
                self.emit(_("can't read the microphone; trying again"))
            mic_down.append(time.monotonic())
            self.stop_event.wait(1.0)
            if hasattr(mic, "reopen"):
                try:
                    mic.reopen()
                    voice_log(f"mikrofon yeniden açıldı: {getattr(mic, 'name', '')}")
                    mic_down.clear()
                except Exception:  # noqa: BLE001
                    pass

        def frames():
            while not self.stop_event.is_set():
                if ptt is not None and ptt.pressed.is_set():
                    self._interrupt()
                    try:
                        audio = ptt.record(mic)
                    except Exception as e:  # noqa: BLE001 - the mic dropped during push-to-talk: the loop must not die
                        mic_failed(e)
                        continue
                    self._utter_q.put((audio, True, None, True, time.monotonic()))
                    continue
                try:
                    frame = mic.read()
                except Exception as e:  # noqa: BLE001
                    mic_failed(e)
                    continue
                if self._mic_health(frame) and hasattr(mic, "reopen"):
                    try:
                        mic.reopen()
                        voice_log(f"mikrofon yeniden açıldı: {getattr(mic, 'name', '')}")
                    except Exception as e:  # noqa: BLE001
                        voice_log(f"mikrofon yeniden açılamadı: {e}")
                # Read it once: another thread can set it to None while Live closes; when that happened between the check
                # and the put, the listening loop crashed and ALICE heard nothing until restarted (2026-11-02).
                live_q = self._live_q
                if (live_q is not None and not self._busy.is_set()
                        and time.monotonic() - self._live_q_at > self.LIVE_Q_ORPHAN):
                    # The queue was opened but the conversation that drains it never started: the mic must not stay deaf.
                    voice_log("canlı konuşma kuyruğu sahipsiz kaldı, mikrofon normale döndü")
                    self._live_q = live_q = None
                if live_q is not None:  # in a live conversation the mic goes to Live (noise filtered)
                    if self.live_gate is not None:
                        self._tune_live_gate()
                    for f in (self.live_gate.feed(frame) if self.live_gate is not None else [frame]):
                        live_q.put(f)
                    continue
                if wake is not None and wake.detect(frame):
                    self._interrupt()
                    self.muted = False
                    self.armed_until = time.monotonic() + self.follow_up
                    self.emit(_("I'm awake"))
                # ALICE must not mistake its own voice for speech while it talks.
                self.vad.boost = 2.5 if self.speaker.speaking else 1.0
                yield frame

        gate = getattr(self.stt, "wake_gate", None)
        if gate is not None and hasattr(gate, "warm_up"):
            gate.warm_up()
        # preroll 20 frames (0.6 s): a quiet "Alice" said right before speech is detected still makes it into the sentence
        for audio in utterances(frames(), self.vad, stop=self.stop_event.is_set, end_silence=self.end_silence,
                                on_start=self._on_speech_start, preroll=20):
            if self._social_skip():
                continue
            started = self._speech_at or None
            direct = self._armed(started)
            audio.ended = time.monotonic()  # latency is measured from the moment the user stopped talking
            self._utter_q.put((audio, direct, started, False, time.monotonic()))

    def _social_skip(self) -> bool:
        """Discord/games: sentences from the open mic don't go to ALICE (push-to-talk only)."""
        why = self.social() if self.social_ptt else ""
        if why and why != self._social_noted:
            voice_log(f"{why}: mikrofon ALICE'e kapalı, sadece bas-konuş ({self.ptt_key})")
        self._social_noted = why
        return bool(why)

    def _mic_health(self, frame: bytes) -> bool:
        """If the mic is completely silent (muted, headset off), ALICE hears nothing and nobody knows why. If there is
        no sound at all for 15 s, say so once and log it; log it again when sound comes back."""
        """Döner: mikrofon akışı yeniden açılmalı mı (dijital sessizlik sürüyor; her 15 sn'de bir dener)."""
        now = time.monotonic()
        if not hasattr(self, "_mic_silent_since"):
            self._mic_silent_since, self._mic_warned, self._mic_retry = now, False, now
        if any(frame):  # a single non-zero sample is enough: it is not digital silence
            if self._mic_warned:
                voice_log("mikrofondan ses geliyor yine")
            self._mic_silent_since, self._mic_warned, self._mic_retry = now, False, now
            return False
        if not self._mic_warned and now - self._mic_silent_since > 15:
            self._mic_warned = True
            voice_log("mikrofon tamamen sessiz: sessize alınmış ya da kapalı olabilir")
            self.emit("mikrofon sessiz")
            self.speaker.say(_("No sound is coming from your microphone; it may be muted."), "serious")
        if now - self._mic_retry > 15:
            self._mic_retry = now
            return True
        return False

    def stop(self) -> None:
        self.stop_event.set()
        self.speaker.stop()


def _gate_time(gate) -> str:
    """How long the wake check took, plus the model wait if there was one ("base 1.20sn, 0.80 bekleme")."""
    total, wait = float(getattr(gate, "last_seconds", 0) or 0), float(getattr(gate, "last_wait", 0) or 0)
    extra = f", {wait:.2f} bekleme" if isinstance(getattr(gate, "last_wait", None), (int, float)) and wait >= 0.1 else ""
    return f"{getattr(gate, 'model_name', '?')} {total:.2f}sn{extra}"


def live_text_options(v: dict[str, Any]) -> dict[str, Any]:
    """[voice] live_text_min: confidence threshold for the local transcript of the first sentence (empty/false =
    always send audio)."""
    m = v.get("live_text_min", -0.7)
    return {"live_text_min": None if m in ("", False, None) else float(m),
            "live_text_wait": float(v.get("live_text_wait", 1.5)),
            "live_follow": float(v.get("live_follow", FOLLOW_MAX)),
            "live_gate": bool(v.get("live_gate", True)),
            "smart_route": bool(v.get("smart_route", True)),
            "social_follow": bool(v.get("social_follow", True))}


def make_live(cfg, agent):
    """The live conversation session when [voice] mode = "live".

    live_backend = "auto": ChatGPT Realtime first (through the local Claude Code proxy; best voice quality),
    otherwise Gemini Live. "chatgpt": ChatGPT only. "gemini": Gemini only."""
    v = cfg.data.get("voice", {})
    if str(v.get("mode", "live")) != "live":
        return None
    backend = str(v.get("live_backend", "auto") or "auto").strip().lower()
    if backend in ("auto", "chatgpt", "realtime", "openai"):
        try:
            from alice.voice.live_chatgpt import RealtimeSession

            rt = RealtimeSession(cfg, agent, log=voice_log)
            if rt.available:
                voice_log(f"canlı konuşma: ChatGPT realtime ({rt.model})")
                return rt
        except Exception as e:  # noqa: BLE001 - no realtime endpoint: fall back to Gemini
            voice_log(f"ChatGPT realtime kullanılamıyor: {e}")
        if backend != "auto":
            return None
    from alice.voice.live import LiveSession

    live = LiveSession(cfg, agent, log=voice_log)
    return live if live.available else None


def build_voice_agent(cfg, inner_ui=None, speaker: Speaker | None = None, cwd=None):
    """Agent for voice conversations: the normal tools + voice instructions."""
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
    vcfg = cfg.data.get("voice", {}) or {}
    if hasattr(speaker, "alert_volume"):
        speaker.volume = float(vcfg.get("volume", 1.0))
        speaker.alert_volume = float(vcfg.get("alert_volume", 0.5))
        # Persisted toggle for flight/game alert audio: defaults to false (muted)
        speaker.alerts_muted = not bool(vcfg.get("game_alerts", False))
    ui = VoiceUI(speaker, inner_ui)
    agent = Agent(cfg, router, all_tools(cfg, ctx.emit), ctx, Permissions(cfg.agent.get("permission_mode", "auto")), ui, decider)
    agent.messages[0]["content"] += "\n\n" + voice_prompt()
    from alice.memory import attach_memory

    attach_memory(agent, "ses")
    if "voice" in cfg.roles:
        agent.role = "voice"
    return agent, speaker, ctx
