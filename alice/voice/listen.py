"""Listening: speech detection, push-to-talk and "Hey Alice".

VAD: webrtcvad if installed, otherwise an energy threshold that adapts to ambient noise.
Wake word: an openWakeWord model if one is given (recommended, fully local). Otherwise, when a short
speech is detected, it is transcribed with Whisper and checked for whether it starts with "alice".
Push-to-talk: a global hotkey via pynput; audio is recorded while the key is held.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Iterator

from alice.voice.audio import Audio, Microphone, rms


class VAD:
    """Is there speech? For 16 kHz frames of 30 ms."""

    def __init__(self, aggressiveness: int = 2, threshold: float = 0.0):
        self.noise = 0.004
        self.threshold = threshold  # 0 = adaptive
        self._levels: deque[float] = deque(maxlen=50)
        self.boost = 1.0            # raised while ALICE speaks so its own echo is not taken for speech
        try:
            import webrtcvad

            self._vad = webrtcvad.Vad(aggressiveness)
        except ImportError:
            self._vad = None

    def is_speech(self, frame: bytes, rate: int = 16000) -> bool:
        level = rms(frame)
        if self._vad is None:
            floor = self.threshold or max(0.01, self.noise * 3.0)
            result = level > floor * self.boost
            if not result:
                self.noise = self.noise * 0.95 + level * 0.05  # slowly track the noise floor
            return result
        # Noise floor = the quietest frame of the last 1.5 s. webrtcvad also counts game/engine/fan noise as speech,
        # so the floor could not be tracked from "quiet" frames and the recording turned into a 30 s noise chunk: a
        # "Hey Alice" said in the middle rather than at the start was missed. Gaps between syllables keep the floor
        # low; with constant noise the floor becomes the noise itself and is not counted as speech.
        self._levels.append(level)
        self.noise = min(self._levels)
        floor = self.threshold or max(0.004, self.noise * 1.8)
        loud = level > floor * self.boost
        try:
            speech = self._vad.is_speech(frame, rate)
        except Exception:
            speech = loud
        return speech and loud


class SileroVAD:
    """Silero VAD (the ONNX model that ships with faster-whisper, no extra download). webrtcvad only checks whether
    there is sound ("ses var mı"), so clicks, breaths, keyboard and game sounds counted as speech and cut ALICE off.
    Silero gives the probability of human speech (~0.1 ms CPU per 32 ms). Same interface as VAD: is_speech(frame),
    boost (the threshold rises while ALICE speaks)."""

    CHUNK = 512
    CONTEXT = 64

    def __init__(self, threshold: float = 0.5, path: str | None = None):
        import numpy as np
        import onnxruntime

        if path is None:
            import faster_whisper

            path = str(Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._np = np
        self._s = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"], sess_options=opts)
        self.threshold = threshold
        self.boost = 1.0
        self.noise = 0.0
        self.prob = 0.0
        self._h = np.zeros((1, 1, 128), dtype="float32")
        self._c = np.zeros((1, 1, 128), dtype="float32")
        self._ctx = np.zeros(self.CONTEXT, dtype="float32")
        self._buf = np.zeros(0, dtype="float32")

    def is_speech(self, frame: bytes, rate: int = 16000) -> bool:
        np = self._np
        x = np.frombuffer(frame, dtype="<i2").astype("float32") / 32768.0
        self._buf = np.concatenate([self._buf, x])
        while len(self._buf) >= self.CHUNK:
            chunk, self._buf = self._buf[:self.CHUNK], self._buf[self.CHUNK:]
            inp = np.concatenate([self._ctx, chunk])[None, :]
            out, self._h, self._c = self._s.run(None, {"input": inp, "h": self._h, "c": self._c})
            self.prob = float(np.asarray(out).ravel()[0])
            self._ctx = chunk[-self.CONTEXT:]
        # While ALICE speaks (boost 2.5) the threshold goes from 0.5 to 0.8,
        # so its own echo and short sounds can't cut in
        thr = min(0.9, self.threshold + 0.2 * (self.boost - 1.0))
        return self.prob >= thr


def make_vad(aggressiveness: int = 2) -> "VAD | SileroVAD":
    """Use Silero if available, otherwise the old VAD (webrtcvad)."""
    try:
        return SileroVAD()
    except Exception:  # noqa: BLE001 - if onnxruntime/faster-whisper is missing, use the old method
        return VAD(aggressiveness)


class SpeechGate:
    """Filters the mic during live speech: when there is no speech, Live gets silence. Game/engine sound
    triggered Live's own speech detection and produced made-up sentences like "vote vote", Japanese or Hindi."""

    # start_frames 8 (~0.24 s of continuous speech): clicks and breaths got through in 3 frames and silenced Live;
    # the 0.3 s before it is still sent, so the first syllable is not cut off.
    def __init__(self, vad: "VAD | None" = None, preroll: int = 10, hangover: int = 20, start_frames: int = 8):
        self.vad = vad or make_vad(3)
        self.ring: deque[bytes] = deque(maxlen=preroll)
        self.hangover = hangover
        self.start_frames = start_frames
        self.voiced = 0
        self.open_left = 0
        # verify: checks whether a sound that cuts in while ALICE speaks (Discord/game) is the user's; the gate
        # opens only after speech builds up to VERIFY_FRAMES and is verified. None = the old behavior.
        self.verify: Callable[[bytes], bool] | None = None
        self._held: list[bytes] = []

    VERIFY_FRAMES = 27  # ~0.8 s: a voice print is unreliable on shorter audio

    def feed(self, frame: bytes) -> list[bytes]:
        """Take a frame and return the frames to send to Live (silence of the same length if there is no speech)."""
        if self.vad.is_speech(frame):
            self.voiced += 1
        else:
            self.voiced = 0
        if self.open_left > 0:
            self.open_left = self.hangover if self.voiced else self.open_left - 1
            return [frame]
        if self._held:
            self._held.append(frame)
            if len(self._held) < self.VERIFY_FRAMES:
                return []  # held until verified (then all go out at once, so timing does not drift)
            held, self._held = self._held, []
            try:
                ok = self.verify is None or self.verify(b"".join(held))
            except Exception:  # noqa: BLE001 - if verification failed, keep the old behavior
                ok = True
            if not ok:  # someone else or ALICE's own echo: don't let it interrupt
                self.voiced = 0
                return [b"\x00" * len(f) for f in held]
            self.open_left = self.hangover
            return held
        if self.voiced >= self.start_frames:  # speech started: send the pre-roll too (first syllable not cut)
            out = list(self.ring) + [frame]
            self.ring.clear()
            if self.verify is not None:
                self._held = out
                return []
            self.open_left = self.hangover
            return out
        self.ring.append(frame)
        return [b"\x00" * len(frame)]


def utterances(frames: Iterator[bytes], vad: VAD, start_frames: int = 6, end_silence: float = 0.8,
               max_seconds: float = 30, preroll: int = 10, rate: int = 16000, frame_len: int = 480,
               stop: Callable[[], bool] = lambda: False, on_start: Callable[[], None] = lambda: None
               ) -> Iterator[Audio]:
    """Extract individual utterances from the frame stream. on_start is called when speech begins (for barge-in)."""
    ring: deque[bytes] = deque(maxlen=preroll)
    voiced = 0
    buf: list[bytes] | None = None
    silent = 0
    end_frames = int(end_silence * rate / frame_len)
    max_frames = int(max_seconds * rate / frame_len)
    for frame in frames:
        if stop():
            return
        speech = vad.is_speech(frame, rate)
        if buf is None:
            ring.append(frame)
            voiced = voiced + 1 if speech else 0
            if voiced >= start_frames:
                buf = list(ring)
                silent = 0
                on_start()
            continue
        buf.append(frame)
        silent = 0 if speech else silent + 1
        if silent >= end_frames or len(buf) >= max_frames:
            yield Audio(b"".join(buf[: len(buf) - silent + 3]), rate)
            buf, voiced = None, 0
            ring.clear()


# Whisper spells "Hey Alice" many ways: "Hey Alis", "Ey Alıs", "Hey Ellis", "Heyalis", "Hey Alize"...
_FILLERS = {"e", "ee", "eee", "şey", "ya", "hmm", "hı", "ıı", "a", "ah", "oh", "pardon"}
_HEYS = {"hey", "hay", "hei", "heyy", "ey", "hi", "he", "hej", "hello", "helo", "selam", "alo",
         "merhaba", "merhabalar", "selamlar", "slm", "gunaydin", "naber", "bak", "baksana"}
_NAMES = {"alice", "alis", "alise", "alize", "aliz", "alisa", "alys", "elis", "ellis", "elise", "ales", "ailis",
          "alıs", "alıse", "aliş", "alış", "allis", "alliss", "alist"}
_TR = str.maketrans("ıİçÇşŞğĞöÖüÜ", "iIcCsSgGoOuU")


_REAL_HEYS = {"hey", "hay", "hei", "heyy", "ey", "hej"}
# Real Turkish names that look like "Alice": "Selam Aliye" must not wake it up.
_NOT_NAMES = {"aliye", "alisan"}


# Continuations starting with "Ali ..." that clearly address ALICE (Turkish characters simplified)
_ALI_CALLS = {"naber", "nbr", "haber", "nasilsin", "nasilsiniz", "selam", "merhaba", "orda", "orada", "burada",
              "burda", "uyan", "uyandin", "duyuyor", "dinliyor", ("ne", "haber"), ("ne", "var"), ("orada", "misin"), ("orda", "misin"), ("beni", "duyuyor"),
              ("duyuyor", "musun")}


def _name_like(word: str, after_hey: bool | str) -> bool:
    import difflib

    w = word.lower().translate(_TR)
    if w in {n.translate(_TR) for n in _NAMES}:
        return True
    # "Hey Ali" is a common mishearing; but "Selam Ali" or "Merhaba Ali" may be said to a real Ali (Discord, room),
    # so only accept it after a real "hey".
    if after_hey and w == "ali":
        return after_hey is True or after_hey in _REAL_HEYS
    if after_hey and w in _NOT_NAMES:
        return False
    # Similarity only after "hey": sentences like "Alicem nerede" alone must not wake it up.
    return after_hey and len(w) >= 4 and max(difflib.SequenceMatcher(None, w, n).ratio() for n in ("alis", "alice", "elis")) >= 0.8


YES_WORDS = {"evet", "tamam", "tamamdır", "olur", "onay", "onayla", "onaylıyorum", "yap", "yapabilirsin", "devam",
             "peki", "tabii", "elbette"}
NO_WORDS = {"hayır", "hayir", "yapma", "dur", "durdur", "iptal", "vazgeç", "istemiyorum", "onaylama", "etme",
            "silme", "açma", "kapatma", "gönderme", "olmaz", "yok", "bekle"}


def is_yes(text: str) -> bool:
    """Is the spoken confirmation a yes? Word-based: "yapma" or "onaylama" must not count as yes (it used to, because
    "yap" matched as a substring). If there is a negative word, the answer is always no."""
    words = re.findall(r"\w+", text.lower())
    if any(w in NO_WORDS for w in words):
        return False
    return any(w in YES_WORDS or w.startswith(("evet", "tamam", "onaylı")) for w in words)


# A name with a suffix is talking about ALICE, not addressing it: "Alice'e de söyle" (tell Alice too), "Alice'dir bu"
# (this is Alice's), "Alice'ten" (from Alice). Affectionate suffixes ("Alice'cim", "Alice'ciğim") are direct address.
_VOCATIVE = re.compile(r"^[cç][ıiuü](?:m|ğ[ıiuü]m)$", re.I)


def _mention(text: str, word: "re.Match[str]") -> bool:
    m = re.match(r"['’](\w+)", text[word.end():])
    return bool(m) and not _VOCATIVE.match(m.group(1))


def strip_wake(text: str, strict: bool = False) -> str | None:
    """'Hey Alice, saat kaç?' (what time is it?) -> 'saat kaç?'. None if there is no wake word.
    A filler word at the start ("ee", "şey") and Whisper's different spellings are allowed.
    strict: the name must be at the start of the sentence ("Bak şimdi Alice ..." does not count);
    used while muted so it does not wake by mistake."""
    text = text.strip()
    words = list(re.finditer(r"\w+", text))
    i = 0
    if i < len(words) and words[i].group().lower() in _FILLERS and len(words) > 1:
        i += 1
    if i < len(words):
        w = words[i].group().lower().translate(_TR)
        # glued spelling such as "Heyalis"
        for h in ("hey", "hay", "ey"):
            if w.startswith(h) and len(w) > len(h) + 2 and _name_like(w[len(h):], True):
                return text[words[i].end():].lstrip(" ,.!?;:-").strip()
    after_hey: bool | str = False
    if i < len(words) and words[i].group().lower().translate(_TR) in _HEYS:
        after_hey = words[i].group().lower().translate(_TR)
        i += 1
        # "Hey hey Alice": a repeated greeting must not push the name off the start (ignored in Discord selective mode)
        while i < len(words) - 1 and words[i].group().lower().translate(_TR) == after_hey:
            i += 1
    # The small model runs "Alice naber" together: "Alisneber", "Alisine ber", "Ali Cennaber" (3 of 6 in the voice test)
    if i < len(words) and i <= 1:
        w = words[i].group().lower().translate(_TR)
        # "Alışanlar", "Alışver", "Alışıyorsun": the "alış-" (with ş) is a real Turkish root, not a glued "Alice"
        # (at least 4 false wake-ups in voice.log). Whisper always writes a glued "Alice" with an s ("Alisneber").
        glued = not re.match(r"al[ıi]ş", words[i].group().lower())
        for pre in ("alice", "alis", "alic", "alıs") if glued else ():
            pre = pre.translate(_TR)
            if w.startswith(pre) and len(w) > len(pre) + 2 and w not in {n.translate(_TR) for n in _NAMES}:
                return (w[len(pre):] + text[words[i].end():]).strip(" ,.!?;:-")
        # "Ali Cennaber": only a long second word starting with c/ç ("Ali Selim" and "Ali Cenk" are real names)
        if (w == "ali" and i + 1 < len(words) and len(words[i + 1].group()) > 5
                and words[i + 1].group().lower().translate(_TR)[:1] in ("c", "j")):
            nxt = words[i + 1].group()
            return (nxt[2:] + text[words[i + 1].end():]).strip(" ,.!?;:-")
    # The user's "Alice" reaches the engines as "Ali": Scribe wrote "Ali? Ne haber?" (Oct 30, real audio).
    # Wake up if "Ali" is alone at the start ("Ali?") or immediately followed by a greeting/call. Sentences said to a
    # real Ali, like "Ali Selim" or "Ali gel buraya", do not wake it up.
    if (i < len(words) and i <= 1 and words[i].group().lower().translate(_TR) == "ali"
            and (not after_hey or after_hey in _REAL_HEYS)):  # "Selam Ali" and "Merhaba Ali" are said to a real Ali
        rest = text[words[i].end():].lstrip(" ,.!?;:-'’").strip()
        nxt = [x.lower().translate(_TR) for x in re.findall(r"\w+", rest)][:3]
        if not nxt or tuple(nxt[:2]) in _ALI_CALLS or nxt[0] in _ALI_CALLS:
            return rest
    # "Alice" split as "Ali C" or "Ali J'e": Ali + one letter (+ suffix)
    if (i < len(words) - 1 and i <= 2 and words[i].group().lower().translate(_TR) == "ali"
            and len(words[i + 1].group()) == 1 and words[i + 1].start() - words[i].end() <= 1):
        rest = text[words[i + 1].end():]
        return re.sub(r"^['’]\w{1,3}", "", rest).lstrip(" ,.!?;:-'’").strip()
    if i < len(words) and i <= 2 and _name_like(words[i].group(), after_hey):
        if _mention(text, words[i]):
            return None
        return re.sub(r"^['’]\w+", "", text[words[i].end():]).lstrip(" ,.!?;:-'’").strip()
    # Like "Bak şimdi Alice ...": the full name within the first three words (exact match, not similarity, so it
    # doesn't wake by mistake). If the name is not at the start, a real request must follow it: sentences like
    # "Şuan Alice ya" or "Gideyse iyi Alice", said while talking about ALICE to someone else, woke it up and got an
    # empty "Buradayım" (I'm here) reply.
    for w in ([] if strict else words[:3]):
        if _name_like(w.group(), False):
            if _mention(text, w):
                return None
            rest = text[w.end():].lstrip(" ,.!?;:-'’").strip()
            meaningful = [x for x in re.findall(r"\w+", rest) if x.lower() not in _FILLERS | {"ya", "yaa", "be", "lan"}]
            return rest if len(meaningful) >= 2 else None
    return None


# Longest time (s) after a reply during which one can talk without saying "Alice". A longer window treated what
# people nearby said as commands; a larger value in the settings file is still capped by this limit.
FOLLOW_MAX = 5.0

# "Alice sus" (Alice, be quiet) and its variants. It used to check a list of exact phrases, which missed natural
# Turkish forms ("sus be", "sussana", "susar mısın", "yeter", "sesini kes", "şşt", "sus sus"), so ALICE did not
# go quiet and answered everything said. Now the sentence is split into words; names, fillers and question suffixes
# are dropped, and every remaining word must be part of a hush phrase. So "sus diye bağırdı" (he yelled "sus") and
# "susam al" (buy sesame) still do not count as hushing.
#
# Strong: silences on its own, and also at the end of a longer sentence (live speech transcript).
_HUSH_RX = re.compile(r"^(?:s+u+s+(?:s?ana|un|sun|ar|abilir|abilirsin|ab|man[ıi]|mak|al[ıi]m|turul)?|ş+t*|s+h+|p+s+t+)$")
_HUSH_PHRASES = {
    ("sessiz", "ol"), ("sessiz", "olur"), ("sessiz", "kal"), ("sessize", "geç"), ("sessize", "al"), ("sessizlik",),
    ("kapa", "çeneni"), ("çeneni", "kapa"), ("çeneni", "kapat"), ("kapat", "çeneni"),
    ("sesini", "kes"), ("kes", "sesini"), ("sesini", "kapat"), ("sesini", "kıs"),
    ("uyku", "moduna", "geç"), ("dinlemeyi", "bırak"), ("beni", "dinleme"), ("konuşmayı", "bırak"),
    ("konuşma", "artık"), ("artık", "konuşma"),
}
# Weak: silences only if it is the whole sentence ("yeter" = enough, "kes" = cut, "dur" = stop), or if a name comes
# right before it ("... Alice yeter"). At the end of a longer sentence it does not silence on its own:
# "pastayı ikiye kes" (cut the cake in half) or "bu kadar yeter mi" (is that enough?) are not commands.
_HUSH_WEAK = {("yeter",), ("kes",), ("dur",), ("konuşma",), ("uyu",), ("bu", "kadar"), ("o", "kadar"),
              ("teşekkürler",), ("teşekkür", "ederim"), ("sağ", "ol"), ("sağol",), ("eyvallah",), ("görüşürüz",),
              ("iyi", "geceler"), ("kes", "artık"), ("bırak",)}
_HUSH_FILLERS = {"tamam", "lütfen", "artık", "bi", "bir", "biraz", "hadi", "şimdi", "ya", "lan", "ok", "okey", "bakayım",
                 "bakim", "bakım", "oke", "ama", "be", "dedim", "diyorum", "sana", "yahu", "canım", "aşkım", "kızım",
                 "bebeğim", "artik", "hemen", "hele", "bakalım", "şu", "an", "mısın", "misin", "musun",
                 "müsün", "mi", "mı", "mu", "mü", "misiniz", "mısınız", "istiyorum", "rica", "ederim", "et",
                 "edebilir", "olur"}


_QUESTION = {"mi", "mı", "mu", "mü", "misin", "mısın", "musun", "müsün", "misiniz", "mısınız"}


def _tr_lower(s: str) -> str:
    """Turkish lowercase: 'SESSİZ' -> 'sessiz'. str.lower turns 'İ' into 'i̇' (dotted combining letter)
    and breaks matching."""
    return s.replace("I", "ı").replace("İ", "i").lower()


def _hush_parse(text: str) -> tuple[str | None, bool]:
    """(kind, has name?). kind: "strong" | "weak" | None. Every word must be part of a hush phrase."""
    words = re.findall(r"\w+", _tr_lower(text))
    named = False
    toks = []
    for w in words:
        if _name_like(w, False) or w.translate(_TR) == "ali":  # Whisper often writes "Alice" as "Ali"
            named = True
            continue
        if w.translate(_TR) in _HEYS or w in _FILLERS:
            continue
        toks.append(w)
    # Like "teşekkür ederim" (thank you) and "susar mısın": if a filler-list word is part of a phrase,
    # check the phrase first.
    kinds: list[str] = []
    i = 0
    while i < len(toks):
        for n in (3, 2, 1):
            seg = tuple(toks[i:i + n])
            if len(seg) < n:
                continue
            if (n == 1 and _HUSH_RX.match(seg[0])) or seg in _HUSH_PHRASES:
                kinds.append("strong")
                i += n
                break
            if seg in _HUSH_WEAK:
                kinds.append("weak")
                i += n
                break
        else:
            if toks[i] in _HUSH_FILLERS:
                i += 1
                continue
            return None, named
    if not kinds:
        return None, named
    if "strong" in kinds:
        return "strong", named
    # A question suffix only goes with a strong word to silence: "susar mısın" (will you be quiet?) yes,
    # "bu kadar yeter mi?" (is that enough?) is a question.
    return (None if any(w in _QUESTION for w in toks) else "weak"), named


def is_hush(text: str) -> bool:
    """"Alice sus" (Alice, be quiet), "sus be", "susar mısın" (will you be quiet?), "yeter" (enough), "sesini kes" (cut
    your voice), "şşt", "tamam Alice teşekkürler" (ok Alice, thanks): is it a hush or end-of-talk sentence? Only if the
    whole sentence is one: "sus diye bağırdı" (he yelled "sus") and "susam al" (buy sesame) do not count."""
    kind, _named = _hush_parse(text)
    return kind is not None


def ends_with_hush(text: str, max_words: int = 6) -> bool:
    """Live speech transcripts arrive in pieces: is all of this turn, or its last few words, a hush? If it is all of it,
    every kind counts; if only the end, then a strong one ("... sus") or one said with a name ("... Alice yeter")."""
    words = re.findall(r"\S+", text)
    if not words:
        return False
    if is_hush(text):
        return True
    for k in range(1, min(max_words, len(words) - 1) + 1):
        kind, named = _hush_parse(" ".join(words[-k:]))
        if kind == "strong" or (kind == "weak" and named):
            return True
    return False


class WakeWord:
    """openWakeWord model (e.g. your own trained hey_alice.onnx). Score per frame."""

    def __init__(self, model_path: str, threshold: float = 0.5):
        from openwakeword.model import Model

        self.model = Model(wakeword_models=[model_path], inference_framework="onnx")
        self.threshold = threshold
        self._buf = b""

    def detect(self, frame: bytes) -> bool:
        # openWakeWord needs chunks of 80 ms (1280 samples).
        import numpy as np

        self._buf += frame
        hit = False
        while len(self._buf) >= 2560:
            chunk, self._buf = self._buf[:2560], self._buf[2560:]
            scores = self.model.predict(np.frombuffer(chunk, dtype=np.int16))
            hit = hit or any(v >= self.threshold for v in scores.values())
        if hit:
            self.model.reset()
        return hit


class PushToTalk:
    """Global hotkey: pressed is True while it is held down. Key polling without a hook on Windows, pynput elsewhere."""

    def __init__(self, key: str = "<scroll_lock>"):
        self.pressed = threading.Event()
        self.released = threading.Event()
        from alice.hotkeys import IS_WIN, KeyPoller

        if IS_WIN:  # no hook: pynput's system-wide keyboard hook slowed down typing everywhere
            def down() -> None:
                self.released.clear()
                self.pressed.set()

            def up() -> None:
                self.pressed.clear()
                self.released.set()

            self._listener = KeyPoller(key, down, up)
            self._listener.start()
            return
        from pynput import keyboard

        name = key.strip("<>").lower()
        self._target = getattr(keyboard.Key, name, None) or keyboard.KeyCode.from_char(name[:1])

        def on_press(k):
            if k == self._target and not self.pressed.is_set():
                self.released.clear()
                self.pressed.set()

        def on_release(k):
            if k == self._target:
                self.pressed.clear()
                self.released.set()

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()

    def record(self, mic: Microphone) -> Audio:
        """Record until the key is released."""
        frames = []
        while self.pressed.is_set():
            frames.append(mic.read())
        return Audio(b"".join(frames), mic.RATE)

    def stop(self) -> None:
        self._listener.stop()
