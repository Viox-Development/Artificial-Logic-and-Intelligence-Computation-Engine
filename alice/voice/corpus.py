"""Real voice corpus: testing the wake decision with the user's real sentences.

Tests written with fake audio check the code's logic, but questions like whether "Alışanlar" wakes ALICE, or whether
a friend's voice on Discord gets through, can only be answered by real recordings. The core sees every sentence's
audio and decision (VoiceAssistant.on_decision); some of them go into the permanent corpus:
- a sentence that woke ALICE is "uyanmalıydı" (should have woken; weak); if no correction comes within 2 minutes,
  it becomes strong,
- if "sana demedim" ("I wasn't talking to you") comes after a wake, it is "uyanmamalıydı" (should not have woken);
  if "yanlış anladın" ("you misunderstood") comes, it is "yanlış döküm" (wrong transcription; strong),
- if Gemini did not hear "Alice", it is "belirsiz" (uncertain; asked first during labeling),
- a voice print far from the user's (another speaker), and the user's sentences without a wake (limited per day),
  are negative examples.

The corpus stays outside the repo (it holds the voices of the user and their friends):
~/.alice/voice/corpus/{audio/DAY/ID.wav, manifest.jsonl}.
Replay recomputes the decision with the real local Whisper (temperature=0, repeatable) and the real voice print;
it looks at the decision, not the text (when the same audio was transcribed twice, Whisper's text changed
about half the time).
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

LABELS = ("uyanmaliydi", "uyanmamaliydi", "belirsiz", "yanlis_dokum")
STRONG = ("kesin", "guclu")
OTHER_MAX = 0.22  # a voice print below this is definitely another speaker (100% repeatable over 30 recordings)
STATE_KEYS = ("direct", "ptt", "armed", "social", "muted", "awaiting")


def root() -> Path:
    """Corpus folder: the ALICE_CORPUS environment variable, or ~/.alice/voice/corpus."""
    env = os.environ.get("ALICE_CORPUS")
    if env:
        return Path(env)
    from alice.config import alice_home

    return alice_home() / "voice" / "corpus"


@dataclass
class Case:
    id: str
    path: str  # relative to the corpus root
    dur: float
    label: str
    bucket: str = ""
    confidence: str = "zayif"  # kesin (confirmed by the user) | guclu (strong) | zayif (weak)
    labeled_by: str = "auto"
    ts: float = 0.0
    state: dict[str, Any] = field(default_factory=dict)
    orig: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def strong(self) -> bool:
        return self.confidence in STRONG

    def wav(self, base: Path) -> Path:
        return base / self.path


def load_manifest(base: Path | None = None) -> list[Case]:
    base = base or root()
    try:
        lines = (base / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    names = set(Case.__dataclass_fields__)
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        out.append(Case(**{k: v for k, v in row.items() if k in names}))
    return out


_lock = threading.Lock()


@contextmanager
def _locked(base: Path, wait: float = 10.0):
    """Manifest lock: threads within one process, and across processes a manifest.lock file (while the core
    collects, "alice voice corpus label"). A lock older than 60 s is treated as abandoned."""
    base.mkdir(parents=True, exist_ok=True)
    lock = base / "manifest.lock"
    with _lock:
        deadline = time.monotonic() + wait
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 60:
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass  # lock vanished or unreadable; try again
                if time.monotonic() > deadline:
                    raise TimeoutError("korpus manifesti kilitli") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)


def save_manifest(cases: list[Case], base: Path | None = None) -> None:
    """Must be called while holding the lock (relabel/remove)."""
    base = base or root()
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f"manifest.jsonl.{os.getpid()}.tmp"
    tmp.write_text("".join(json.dumps(asdict(c), ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
    for i in range(5):
        try:
            tmp.replace(base / "manifest.jsonl")
            return
        except PermissionError:  # Windows: the file is being read by another process right now
            if i == 4:
                raise
            time.sleep(0.1)


def add(audio, label: str, *, bucket: str = "", confidence: str = "zayif", state: dict | None = None,
        orig: dict | None = None, note: str = "", case_id: str = "", base: Path | None = None,
        labeled_by: str = "auto") -> Case:
    """Copy the audio into the corpus (16 kHz) and add it to the manifest. audio: an Audio or a wav path."""
    from alice.voice.audio import Audio

    base = base or root()
    if not isinstance(audio, Audio):
        audio = Audio.load(audio)
    audio = audio.resample(16000)
    cid = case_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.monotonic() * 1000) % 1000:03d}"
    with _locked(base):
        rel = f"audio/{cid[:8]}/{cid}.wav"
        n = 1
        while (base / rel).exists():  # same millisecond: keep IDs unique so labels don't land on the wrong take
            n += 1
            rel = f"audio/{cid[:8]}/{cid}-{n}.wav"
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        audio.save(base / rel)
        case = Case(Path(rel).stem, rel, round(audio.seconds, 2), label, bucket, confidence, labeled_by, time.time(),
                    {k: (state or {}).get(k) for k in STATE_KEYS if k in (state or {})}, dict(orig or {}), note)
        with (base / "manifest.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(case), ensure_ascii=False) + "\n")
    return case


def relabel(case_id: str, base: Path | None = None, **fields: Any) -> bool:
    base = base or root()
    with _locked(base):
        cases = load_manifest(base)
        hit = next((c for c in cases if c.id == case_id), None)
        if hit is None:
            return False
        for k, v in fields.items():
            setattr(hit, k, v)
        save_manifest(cases, base)
    return True


def remove(case_id: str, base: Path | None = None) -> bool:
    base = base or root()
    with _locked(base):
        cases = load_manifest(base)
        keep = [c for c in cases if c.id != case_id]
        if len(keep) == len(cases):
            return False
        for c in cases:
            if c.id == case_id:
                c.wav(base).unlink(missing_ok=True)
        save_manifest(keep, base)
    return True


# ---------------------------------------------------------------------------------------------- collection
def correction_label(text: str) -> str | None:
    """What a correction means for the wake. Only explicit phrases give a label: "sana demedim" ("I wasn't talking
    to you") -> uyanmamalıydı; "yanlış anladın / onu demedim" ("you misunderstood / I didn't say that") -> yanlış döküm
    (the wake was right). Task corrections such as "Hayır, YouTube'da" ("No, on YouTube"), "saçmaladın" ("you
    talked nonsense") or "yapmadın ki" ("you didn't do it") say nothing about the wake: None."""
    from alice.voice.lang import misheard, not_for_me

    if text.lower().startswith("[gemini"):
        return "belirsiz"
    if not_for_me(text):
        return "uyanmamaliydi"
    if misheard(text):
        return "yanlis_dokum"
    return None


class Collector:
    """Attached to VoiceAssistant in the core: on_decision and on_correct."""

    CONFIRM_AFTER = 120.0  # s: if no correction arrives this long after a wake, "uyanmalıydı" becomes strong
    POS_PER_DAY = 100  # wakes, at most per day
    NEG_PER_DAY = 40  # the user's sentences without a wake (negative examples), at most per day
    OTHER_PER_DAY = 10  # other speakers, at most per day
    MAX_CASES = 3000  # ~40 KB per recording: at most ~120 MB

    def __init__(self, base: Path | None = None, log: Callable[[str], None] = lambda s: None):
        self.base = base or root()
        self.log = log
        # (time, id, heard text): wakes waiting for a correction
        self._pending: list[tuple[float, str, str]] = []
        self._day = ""
        self._counts = {"pos": 0, "neg": 0, "other": 0}
        self._total: int | None = None

    def _count(self) -> int:
        """Number of recordings in the corpus, read from the manifest so the cap survives a process restart."""
        if self._total is None:
            self._total = len(load_manifest(self.base))
        return self._total

    def _room(self, kind: str) -> bool:
        day = time.strftime("%Y%m%d")
        if day != self._day:
            # Today's count comes from the manifest: when it reset on every core restart, the daily limit was
            # not enforced (96 recordings a day instead of 40).
            today = [c for c in load_manifest(self.base) if c.id.startswith(day)]
            kinds = {"pozitif": "pos", "negatif-konusma": "neg", "baska-konusmaci": "other"}
            self._day, self._counts = day, {"pos": 0, "neg": 0, "other": 0}
            for c in today:
                if c.bucket in kinds:
                    self._counts[kinds[c.bucket]] += 1
        limit = {"pos": self.POS_PER_DAY, "neg": self.NEG_PER_DAY}.get(kind, self.OTHER_PER_DAY)
        return self._counts[kind] < limit and self._count() < self.MAX_CASES

    def _add(self, kind: str, audio, label: str, path=None, **kw) -> Case:
        c = add(audio, label, base=self.base, case_id=Path(path).stem if path else "", **kw)
        self._counts[kind] += 1
        self._total = self._count() + 1
        return c

    def _settle(self, now: float) -> None:
        while self._pending and now - self._pending[0][0] > self.CONFIRM_AFTER:
            try:
                relabel(self._pending[0][1], self.base, confidence="guclu", note="düzeltme gelmedi")
            except Exception:  # noqa: BLE001 - lock/disk: retry on the next decision
                return
            self._pending.pop(0)

    def on_decision(self, audio, d: dict[str, Any], path=None) -> None:
        now = time.monotonic()
        self._settle(now)
        reason, spk, head = d.get("reason"), d.get("spk"), str(d.get("head") or "")
        orig = {"reason": reason, "spk": spk, "head": head, "path": d.get("path"), "recent": str(path or "")}
        thr = d.get("thr")
        if d.get("ptt") or reason in ("cevap", "kisa", "?"):
            return  # push-to-talk and confirmation replies are not wake decisions
        if d.get("woke") and reason in ("uyandi", "takip", "susturma"):
            if self._room("pos"):
                c = self._add("pos", audio, "uyanmaliydi", path, bucket="pozitif", state=d, orig=orig)
                self._pending.append((now, c.id, head))
        elif reason == "ses_izi" and spk is not None and spk < OTHER_MAX and self._room("other"):
            orig["head"] = ""  # someone else's words are not stored
            self._add("other", audio, "uyanmamaliydi", path, bucket="baska-konusmaci", confidence="guclu",
                      state=d, orig=orig)
        elif (reason == "uyandirma_yok" and head.strip() and spk is not None and thr is not None and spk >= thr
              and self._room("neg")):
            # Only sentences whose voice print belongs to the user: words from a gray-zone or unrecognized voice
            # are not stored.
            self._add("neg", audio, "uyanmamaliydi", path, bucket="negatif-konusma", state=d, orig=orig)

    def on_correct(self, text: str) -> None:
        """The last wake was corrected (see correction_label). The correction sentence itself may also have been
        recorded as a wake (after the session closed, "Alice sana demedim"): the label goes to the wake before it."""
        from alice.voice.lang import correction

        label = correction_label(text)
        if label is None or not self._pending:
            return
        while self._pending and correction(self._pending[-1][2]):
            _, cid, _ = self._pending.pop()  # correction sentence: said to ALICE, a correct wake
            relabel(cid, self.base, confidence="guclu", note="düzeltme cümlesi")
        if not self._pending:
            return
        _, cid, _ = self._pending.pop()
        fields: dict[str, Any] = {"label": label, "note": text[:160],
                                  "confidence": "zayif" if label == "belirsiz" else "guclu"}
        if label == "uyanmamaliydi":
            fields["bucket"] = "negatif-konusma"
        relabel(cid, self.base, **fields)
        self.log(f"korpus: {cid} -> {label} ({text[:60]!r})")


def attach(va, base: Path | None = None, log: Callable[[str], None] = lambda s: None) -> Collector:
    c = Collector(base, log)
    va.on_decision = c.on_decision
    va.on_correct = c.on_correct
    return c


# --------------------------------------------------------------------------------------- replay
@dataclass
class Decision:
    woke: bool
    reason: str
    spk: float | None
    head: str
    ms: int


class _Agent:
    ui = None

    def run_turn(self, text):
        return ""

    def stop(self):
        pass


class _Speaker:
    speaking = False
    last_cut = None

    def say(self, text, emotion=None, **kw):
        return True

    def stop(self):
        pass

    def wait(self, timeout=None):
        pass


class _Live:
    available = True

    def __init__(self, names: list[str]):
        self._names = names

    def names(self):
        return self._names

    def prefetch(self, owner=None):
        pass

    def discard(self, owner=None):
        pass

    def converse(self, *a, **k):
        pass


class _Sink:
    def put(self, item):
        pass


def _deterministic(gate) -> None:
    """Turn off Whisper's temperature fallback: the same audio always gives the same text (this gate only)."""
    load = gate._load

    def loaded():
        model = load()
        inner = model.transcribe

        def transcribe(*a, **k):
            k["temperature"] = 0.0
            return inner(*a, **k)

        model.transcribe = transcribe
        return model

    gate._load = loaded


def build_replay_assistant(*, home: Path | None = None, wake_model: str = "base", threshold: float | None = None,
                           deterministic: bool = True, names: list[str] | None = None, speaker: bool = True):
    """A VoiceAssistant with the real local Whisper and real voice print, but a fake agent, speaker and Live.
    Nothing is spoken and nothing goes to the cloud (second ear off). home: ~/.alice/voice, which holds the
    profile and models."""
    from types import SimpleNamespace

    from alice.voice import assistant as A
    from alice.voice import speaker_id as sid
    from alice.voice.stt import WakeGate

    A.voice_log = lambda line: None  # replay must not pollute the real voice.log (this process only)
    if home is None:
        from alice.config import alice_home

        home = alice_home() / "voice"
    gate = WakeGate(wake_model, "tr")
    if deterministic:
        _deterministic(gate)
    stt = SimpleNamespace(wake_gate=gate, engines=[], second_ear=None, last="yerel")
    stt.transcribe = lambda audio: (gate.transcribe(audio) or ("", 0.0))[0]
    spk = None
    if speaker:
        profile = sid.load_profile(home / "speaker.json")
        if profile:
            if threshold is None:
                try:
                    from alice.config import load_config

                    threshold = (load_config().data.get("voice") or {}).get("speaker_threshold")
                except Exception:  # noqa: BLE001
                    threshold = None
            spk = sid.SpeakerGate(profile, sid.Embedder(home / "models" / sid.MODEL_NAME),
                                  threshold=float(threshold) if threshold is not None else None)
    if names is None:
        try:
            from alice.config import load_config
            from alice.voice.vocab import known_names

            names = known_names(load_config())
        except Exception:  # noqa: BLE001
            names = []
    va = A.VoiceAssistant(_Agent(), _Speaker(), stt, live=_Live(names), speaker_gate=spk)
    va._jobs = _Sink()  # after a wake, don't go to Live: the decision is enough
    return va


def replay(case: Case, va, base: Path | None = None) -> Decision:
    """Replay the recording with the ALICE state it had when recorded, and return the decision."""
    from alice.voice.audio import Audio

    base = base or root()
    st = case.state or {}
    now = time.monotonic()
    va.muted = bool(st.get("muted"))
    va.armed_until = now + 4.0 if st.get("armed") else 0.0
    va._called_until = va.armed_until
    social = str(st.get("social") or "")
    va.social = lambda: social
    va._awaiting_answer = bool(st.get("awaiting"))
    va._unwoken, va._score, va._barge = None, None, False
    t0 = time.monotonic()
    va.handle_audio(Audio.load(case.wav(base)), direct=bool(st.get("direct")), ptt=bool(st.get("ptt")))
    d = va.decision
    va.muted, va._awaiting_answer = False, False
    return Decision(bool(d.get("woke")), str(d.get("reason")), d.get("spk"), str(d.get("head") or ""),
                    int((time.monotonic() - t0) * 1000))


def expected(case: Case) -> bool | None:
    """Should this recording wake ALICE? (None when uncertain)"""
    if case.label in ("uyanmaliydi", "yanlis_dokum"):
        return True
    if case.label == "uyanmamaliydi":
        return False
    return None


def evaluate(cases: list[Case], va, base: Path | None = None, runs: int = 1,
             progress: Callable[[int, int], None] = lambda i, n: None) -> dict[str, Any]:
    """Replay them all; metrics and per-case results."""
    rows = []
    for i, c in enumerate(cases):
        progress(i, len(cases))
        ds = [replay(c, va, base) for _ in range(max(1, runs))]
        woke = sum(d.woke for d in ds) * 2 > len(ds)
        rows.append({"id": c.id, "label": c.label, "bucket": c.bucket, "confidence": c.confidence,
                     "expect": expected(c), "woke": woke, "reason": ds[0].reason, "spk": ds[0].spk,
                     "unstable": len({d.woke for d in ds}) > 1, "ms": ds[0].ms, "dur": c.dur})
    return {"rows": rows, **metrics(rows)}


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    neg = [r for r in rows if r["expect"] is False]
    pos = [r for r in rows if r["expect"] is True]
    neg_strong = [r for r in neg if r["confidence"] in STRONG]
    is_other = [r["bucket"] == "baska-konusmaci" and r["expect"] is False for r in rows]
    user = [r["spk"] for r, o in zip(rows, is_other) if r["spk"] is not None and not o]
    other = [r["spk"] for r, o in zip(rows, is_other) if r["spk"] is not None and o]
    neg_hours = sum(r["dur"] for r in neg) / 3600
    ms = sorted(r["ms"] for r in rows)
    return {
        "n": len(rows),
        "false_wakes": [r["id"] for r in neg if r["woke"]],
        "false_wakes_strong": [r["id"] for r in neg_strong if r["woke"]],
        "neg": len(neg), "neg_hours": neg_hours,
        "missed": [r["id"] for r in pos if not r["woke"]], "pos": len(pos),
        "user_min": min(user) if user else None, "other_max": max(other) if other else None,
        "unstable": [r["id"] for r in rows if r["unstable"]],
        "p50_ms": ms[len(ms) // 2] if ms else 0, "p95_ms": ms[int(len(ms) * 0.95)] if ms else 0,
    }


def format_report(rep: dict[str, Any], threshold: float | None = None) -> str:
    rows = rep["rows"]
    conf = {k: sum(r["confidence"] == k for r in rows) for k in ("kesin", "guclu", "zayif")}
    unsure = sum(r["expect"] is None for r in rows)
    lines = [f"Korpus: {rep['n']} kayıt (kesin {conf['kesin']}, güçlü {conf['guclu']}, zayıf {conf['zayif']}, "
             f"belirsiz {unsure})" + (f", ses izi eşiği {threshold:.2f}" if threshold is not None else "")]
    fa_h = len(rep["false_wakes"]) / rep["neg_hours"] if rep["neg_hours"] else 0.0
    lines.append(f"Yanlış uyanma : {len(rep['false_wakes'])}/{rep['neg']} (güçlülerde {len(rep['false_wakes_strong'])}; "
                 f"saatte {fa_h:.1f}, olumsuz ses {rep['neg_hours'] * 60:.1f} dk)")
    if rep["pos"]:
        lines.append(f"Kaçan uyanma  : {len(rep['missed'])}/{rep['pos']} (%{100 * len(rep['missed']) / rep['pos']:.0f})")
    else:
        lines.append("Kaçan uyanma  : ölçülemiyor (korpusta henüz 'uyanmalıydı' kaydı yok)")
    if rep["user_min"] is not None or rep["other_max"] is not None:
        um = f"{rep['user_min']:.3f}" if rep["user_min"] is not None else "-"
        om = f"{rep['other_max']:.3f}" if rep["other_max"] is not None else "-"
        lines.append(f"Ses izi       : kullanıcı en düşük {um} | başka biri en yüksek {om}")
    lines.append(f"Kararsız      : {len(rep['unstable'])}/{rep['n']}")
    lines.append(f"Süre          : kayıt başına p50 {rep['p50_ms'] / 1000:.1f} sn, p95 {rep['p95_ms'] / 1000:.1f} sn")
    bad = [r for r in rows if r["expect"] is not None and r["woke"] != r["expect"]]
    for r in bad[:30]:
        want = "uyanmalıydı" if r["expect"] else "uyanmamalıydı"
        spk = f", ses izi {r['spk']:.3f}" if r["spk"] is not None else ""
        lines.append(f"  ✗ {r['id']}  {want}, karar: {r['reason']}{spk} ({r['confidence']})")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------- import, labeling
def label_from_decision(d: dict[str, Any]) -> tuple[str, str, str]:
    """(label, bucket, confidence) from an old recording's decision. Whether a recording that woke was right is
    unknown: uncertain."""
    spk, head = d.get("spk"), str(d.get("head") or "").strip()
    if d.get("woke"):
        return "belirsiz", "pozitif", "zayif"
    if spk is not None and spk < OTHER_MAX:
        return "uyanmamaliydi", "baska-konusmaci", "guclu"
    if d.get("reason") in ("ses_izi", "gri", "stt_hata"):
        return "belirsiz", "negatif-konusma", "zayif"  # content unknown: asked during labeling
    if not head:
        return "uyanmamaliydi", "konusma-yok", "guclu"
    return "uyanmamaliydi", "negatif-konusma", "zayif"


def import_recent(va=None, recent: Path | None = None, base: Path | None = None,
                  log: Callable[[str], None] = lambda s: None) -> int:
    """Import the latest recordings in ~/.alice/voice/recent into the corpus (replayed when the side file has no
    decision)."""
    from alice.config import alice_home

    recent = recent or alice_home() / "voice" / "recent"
    base = base or root()
    cases = load_manifest(base)
    have = {c.id for c in cases} | {Path(c.orig["recent"]).stem for c in cases if c.orig.get("recent")}
    n = 0
    for wav in sorted(recent.glob("*.wav")):
        if wav.stem in have:
            continue
        side = wav.with_suffix(".json")
        d: dict[str, Any] = {}
        if side.exists():
            try:
                d = json.loads(side.read_text(encoding="utf-8"))
            except ValueError:
                d = {}
        if not d and va is not None:
            tmp = Case(wav.stem, str(wav), 0.0, "belirsiz")
            r = replay(tmp, va, base=Path("/"))
            d = {"woke": r.woke, "reason": r.reason, "spk": r.spk, "head": r.head}
        label, bucket, conf = label_from_decision(d)
        orig = {k: d.get(k) for k in ("reason", "spk", "head", "path")}
        if bucket == "baska-konusmaci":
            orig["head"] = ""
        add(wav, label, bucket=bucket, confidence=conf, state=d, orig=orig, case_id=wav.stem, base=base,
            labeled_by="içe-alma")
        log(f"{wav.stem}: {label} ({bucket}, {conf})")
        n += 1
    return n


def play(path: Path) -> None:
    try:
        import sounddevice as sd

        from alice.voice.audio import Audio

        a = Audio.load(path)
        sd.play(__import__("numpy").frombuffer(a.pcm, dtype="<i2"), a.rate)
        sd.wait()
    except Exception:  # noqa: BLE001 - if the audio can't be played, only the question is asked
        pass


def to_label(order: list[Case]) -> list[Case]:
    """To be labeled: uncertain ones first, then weak ones (newest first)."""
    rank = {"belirsiz": 0}
    return sorted([c for c in order if c.labeled_by != "kullanıcı"],
                  key=lambda c: (rank.get(c.label, 1), c.confidence != "zayif", -c.ts))
