"""Reflex döngüsü: System One ile hızlı arayüz gezintisi.

Her adım:
  1. observe  -> ekrandaki öğeler ID'li listeye çevrilir
  2. rank     -> hedefle en alakalı N aday seçilir (Jev'e az ve net seçenek gitsin)
  3. decide   -> tek System One çağrısı: "sıradaki aksiyon?" + "hedef tamam mı?". Jev görsel görebiliyorsa
                (TypeSafe anahtarı, [decider] jev_images) pencerenin görüntüsü de gider ve aynı istekte
                "listede olmayan ama ekranda görünen bir şeye tıkla" seçeneği için hedefin var olup olmadığı (Noul)
                ve yeri (Box) da sorulur: içi okunamayan pencerelerde (WebView, oyun menüsü) de tıklanabilsin.
  4. act      -> seçilen aday uygulanır; metin gerekiyorsa hızlı LLM üretir
  5. düşük güven / aynı aksiyonun tekrarı / 'stuck' -> beyne (System 2) geri döner

Planlama yok, uzun düşünme yok: bu döngü bilinen bir hedefe giden kısa tıklama
dizileri için. Karmaşık işlerde beyin modeli bu döngüyü `reflex` aracıyla parça
parça çağırır.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from alice.decider import Box, Choice, Decider, DeciderError, Noul, padded_fraction, prepare_image
from alice.reflex.observers import Element, Observation, Observer

SPECIAL = {
    "scroll_down": "Scroll down to reveal more elements",
    "scroll_up": "Scroll up",
    "press_enter": "Press Enter to submit what was just typed",
    "back": "Go back to the previous page/screen",
    "done": "The goal is already fully achieved, stop",
    "stuck": "None of the options make progress toward the goal",
}

VISUAL_KEY = "click_on_screen"
VISUAL_OPTION = "Click a button, link, tab, field or menu item that is visible in `screen` but is not one of these options"
TARGET = "the button, link, tab, field or menu item in `screen` that a person would click next to work toward `goal`"
TARGET_VISIBLE = ("Is there a button, link, tab, field or menu item in `screen` that a person would click next to "
                  "work toward `goal`?")
VISIBLE_MIN = 0.7  # "hedef ekranda var mı?" bundan düşükse Box'a güvenilmez (Box her zaman bir kutu döndürür)
SCREEN_REF = "alice-screen-6f1c0d2e"  # görselin istekteki yer tutucusu: metinde kazara geçemeyecek bir dize

_WORD = re.compile(r"[\wçğıöşüÇĞİÖŞÜ]{3,}", re.U)


def _words(s: str) -> set[str]:
    return {w.lower().replace("ı", "i").replace("İ", "i") for w in _WORD.findall(s)}


def rank(elements: list[Element], goal: str, limit: int, typed: set[int]) -> list[Element]:
    gw = _words(goal)
    scored = []
    for order, el in enumerate(elements):
        if not el.label and not el.editable:
            continue
        overlap = len(gw & _words(el.label + " " + el.value))
        score = overlap * 3 + (2 if el.editable and el.id not in typed else 0) + (1 if el.in_view else 0)
        scored.append((-score, order, el))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [el for _, _, el in scored[:limit]]


def _named_in_goal(key: str, by_key: dict[str, Element], elements: list[Element], goal: str) -> bool:
    """Seçilen tıklama, hedefte adı geçen ve ekranda o adı taşıyan TEK öğe mi? ("ekrandaki Impossible'a tıkla")
    Öyleyse karar motorunun güveni düşük olsa da (tıkla mı arama kutusuna mı yaz, kararsız kalıyor) uygula."""
    el = by_key.get(key)
    if el is None or not key.startswith("click_"):
        return False
    gw = _words(goal)

    def hits(e: Element) -> int:
        return len(gw & _words(e.label))

    best = hits(el)
    if best == 0:
        return False
    # Aynı şey sayfada birkaç kez geçebilir (başlık, kapak resmi, "oynat" düğmesi): hepsi aynı öğeyse sorun yok.
    rivals = [_words(e.label) & gw for e in elements if e.id != el.id and hits(e) >= best]
    return all(r == (_words(el.label) & gw) for r in rivals)


@dataclass
class ReflexResult:
    status: str  # done | needs_help | max_steps | error
    steps: int
    reason: str
    log: list[str] = field(default_factory=list)
    title: str = ""
    location: str = ""

    def report(self) -> str:
        head = f"reflex: {self.status} ({self.steps} adım) - {self.reason}\nekran: {self.title} | {self.location}"
        return head + ("\nadımlar:\n" + "\n".join(self.log[-15:]) if self.log else "")


class ReflexLoop:
    def __init__(
        self,
        observer: Observer,
        decider: Decider,
        text_writer: Callable[[str, str], str] | None = None,
        min_confidence: float = 0.55,
        max_candidates: int = 40,
        emit: Callable[[str], None] = lambda s: None,
        settle: float = 0.3,
    ):
        self.obs = observer
        self.decider = decider
        self.text_writer = text_writer
        self.min_conf = min_confidence
        self.max_cands = max_candidates
        self.emit = emit
        self.settle = settle
        self.visual = True  # görsel istek bir kez reddedilirse (izin yok, görüntü kabul edilmedi) bu çalışmada kapanır

    def _look(self) -> tuple[bytes, tuple[float, float]] | None:
        """Pencerenin Jev'e gidecek görüntüsü ve Box kesirlerini asıl görüntüye çevirme çarpanı; olmuyorsa None."""
        snap = getattr(self.obs, "snapshot", None)
        if not (self.visual and callable(snap) and getattr(self.decider, "supports_images", False)):
            return None
        try:
            img = snap()
        except Exception as e:  # noqa: BLE001 - görüntü alınamadı: görselsiz devam
            self.emit(f"ekran görüntüsü alınamadı, görselsiz devam: {e}")
            return None
        if img is None:
            return None
        return prepare_image(img), padded_fraction(img.size)

    def run(self, goal: str, max_steps: int = 25, inputs: dict[str, str] | None = None) -> ReflexResult:
        if hasattr(self.obs, "goal"):
            self.obs.goal = goal  # pencere çerçevesi süzgeci "kapat" gibi hedefleri bilsin
        log: list[str] = []
        typed: set[int] = set()
        last_keys: list[str] = []
        ob = Observation("", "", [])
        for step in range(1, max_steps + 1):
            try:
                ob = self.obs.observe()
            except Exception as e:  # noqa: BLE001
                return ReflexResult("error", step - 1, f"gözlem hatası: {e}", log)
            cands = rank(ob.elements, goal, self.max_cands, typed)
            options: dict[str, str] = {}
            by_key: dict[str, Element] = {}
            for el in cands:
                key = f"{'type' if el.editable else 'click'}_{el.id}"
                options[key] = f"{'Type into' if el.editable else 'Click'} {el.describe()}"
                by_key[key] = el
            options.update(SPECIAL)
            look = self._look()
            t0 = time.monotonic()
            d = None
            if look is not None:
                # Aynı isteğe hem sıradaki adım hem "ekranda tıklanacak şey var mı, nerede?" (spekülatif soru:
                # cevabı sadece click_on_screen seçilirse kullanılır).
                state = {"goal": goal, "window": f"{ob.title} | {ob.location}", "screen": SCREEN_REF,
                         "recent_actions": log[-8:]}
                try:
                    d = self.decider.decide(state, {
                        "next": Choice("Which single next action moves closest to `goal`?",
                                       {**options, VISUAL_KEY: VISUAL_OPTION}),
                        "done": Noul("Looking at `screen` and `recent_actions`, is `goal` already fully achieved?"),
                        "target_visible": Noul(TARGET_VISIBLE),
                        "target": Box(TARGET),
                    }, images={SCREEN_REF: look[0]})
                except DeciderError as e:
                    self.visual = False
                    self.emit(f"görsel karar olmadı, görselsiz devam: {e}")
            if d is None:
                state = (
                    f"GOAL: {goal}\nSCREEN: {ob.title} | {ob.location}\n"
                    f"ELEMENTS: {len(ob.elements)} on screen, {len(cands)} shown as options\n"
                    "RECENT ACTIONS (oldest first):\n" + ("\n".join(log[-8:]) or "(none yet)")
                )
                try:
                    d = self.decider.decide(state, {
                        "next": Choice("Which single next action moves closest to the GOAL?", options),
                        "done": Noul("Looking at the SCREEN and RECENT ACTIONS, is the GOAL already fully achieved?"),
                    })
                except DeciderError as e:
                    return ReflexResult("error", step - 1, f"karar motoru hatası: {e}", log, ob.title, ob.location)
            nxt, done = d["next"], d["done"]
            self.emit(f"[{step}] {nxt.value} (güven {nxt.confidence:.2f}, bitti? {done.value:.2f}) "
                      f"{(time.monotonic() - t0) * 1000:.0f} ms via {d.engine}")

            if done.value >= 0.85 or nxt.value == "done":
                return ReflexResult("done", step - 1, "hedefe ulaşıldı", log, ob.title, ob.location)
            if nxt.value == "stuck":
                return ReflexResult("needs_help", step - 1, "uygun seçenek yok", log, ob.title, ob.location)
            if nxt.value == VISUAL_KEY:
                # Box her zaman bir kutu döndürür: "ekranda var mı?" düşükse kutuya güvenme, beyne bırak.
                seen = d["target_visible"].value
                if seen < VISIBLE_MIN:
                    return ReflexResult("needs_help", step - 1, f"ekranda tıklanacak şey bulunamadı (var mı? {seen:.2f})",
                                        log, ob.title, ob.location)
                b = d["target"].value
                sx, sy = look[1] if look else (1.0, 1.0)
                fx = min(1.0, (b["left"] + b["right"]) / 2 * sx)
                fy = min(1.0, (b["top"] + b["bottom"]) / 2 * sy)
                last_keys.append(f"{VISUAL_KEY}@{fx:.2f},{fy:.2f}")
                if len(last_keys) >= 3 and len(set(last_keys[-3:])) == 1:
                    return ReflexResult("needs_help", step - 1, "aynı yere tekrar tekrar tıklanıyor",
                                        log, ob.title, ob.location)
                try:
                    self.obs.click_at(fx, fy)
                    log.append(f"{step}. ekranda tıklandı ({fx:.2f}, {fy:.2f}) var mı? {seen:.2f}")
                except Exception as e:  # noqa: BLE001
                    log.append(f"{step}. ekranda tıklama -> HATA {e}")
                time.sleep(self.settle)
                continue
            if nxt.confidence < self.min_conf and not _named_in_goal(nxt.value, by_key, ob.elements, goal):
                return ReflexResult("needs_help", step - 1,
                                    f"düşük güven ({nxt.confidence:.2f}) - '{options.get(nxt.value, nxt.value)}'",
                                    log, ob.title, ob.location)
            last_keys.append(nxt.value)
            if len(last_keys) >= 3 and len(set(last_keys[-3:])) == 1:
                return ReflexResult("needs_help", step - 1, f"aynı aksiyon tekrarlanıyor: {nxt.value}",
                                    log, ob.title, ob.location)

            try:
                entry = self._act(nxt.value, by_key, goal, inputs or {}, typed, options)
            except Exception as e:  # noqa: BLE001
                log.append(f"{step}. {nxt.value} -> HATA {e}")
                continue
            log.append(f"{step}. {entry}")
            time.sleep(self.settle)
        return ReflexResult("max_steps", max_steps, "adım limiti doldu", log, ob.title, ob.location)

    def _act(self, key: str, by_key: dict[str, Element], goal: str, inputs: dict[str, str],
             typed: set[int], options: dict[str, str]) -> str:
        if key == "scroll_down":
            self.obs.scroll(True)
        elif key == "scroll_up":
            self.obs.scroll(False)
        elif key == "back":
            self.obs.back()
        elif key == "press_enter":
            self.obs.press_enter()
        elif key.startswith("click_"):
            self.obs.click(by_key[key].id)
        elif key.startswith("type_"):
            el = by_key[key]
            text = self._text_for(el, goal, inputs)
            self.obs.type(el.id, text, submit=False)
            typed.add(el.id)
            return f"{options[key]} <- '{text}'"
        return options.get(key, key)

    def _text_for(self, el: Element, goal: str, inputs: dict[str, str]) -> str:
        label = el.label.lower()
        for k, v in inputs.items():
            if k.lower() in label:
                return v
        if len(inputs) == 1:
            return next(iter(inputs.values()))
        if self.text_writer is None:
            raise DeciderError("metin üretici yok")
        return self.text_writer(goal, el.describe())


def make_text_writer(router) -> Callable[[str, str], str]:
    """System One metin üretmediği için yazılacak metni hızlı LLM'e ürettir."""

    def write(goal: str, field_desc: str) -> str:
        res = router.chat("fast", [
            {"role": "system", "content": "Return ONLY the exact text to type into the field. No quotes, no explanation."},
            {"role": "user", "content": f"Goal: {goal}\nField: {field_desc}\nText to type:"},
        ], temperature=0, max_tokens=200)
        return res.text.strip().strip('"').strip("'").splitlines()[0] if res.text.strip() else ""

    return write
