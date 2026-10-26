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
from dataclasses import dataclass, field, replace
from typing import Any, Callable

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
ZOOM_REF = "alice-zoom-3b8e91a7"
ZOOM_MIN = 0.4  # "var mı?" en az bu kadarsa kutunun çevresi yakından bir kez daha sorulur
ZOOM_SPAN = (0.3, 0.4)  # yakın görüntü en az pencerenin bu kadarı (genişlik, yükseklik)

# Hedef tek bir tıklamaysa ("ilk projeye tıkla") ekranda görülen hedefe tıklanınca iş biter. Önceden Jev'in
# "bitti mi?" sorusu tıklananın ne olduğunu bilmediği için düşük kalıyor, aynı yere üç kez tıklanıp "yapılamadı"
# deniyordu. Sonraki adım içeren hedefler (yaz, gönder, sonra...) bu kısa yoldan geçmez.
_CLICK_GOAL = re.compile(r"tıkla|tikla|click|\bbas\b|basar|\bseç\b|seçer|\baç\b", re.I)
_MULTI_STEP = re.compile(r"\byaz|gir\b|doldur|gönder|kaydet|sonra|ardından|\bve\b|,|then|\band\b|type|"
                         r"\bara\b|arat|indir|kopyala|yapıştır", re.I)


def single_click_goal(goal: str) -> bool:
    return bool(_CLICK_GOAL.search(goal)) and not _MULTI_STEP.search(goal)


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
    # done | clicked (tek tıklamalık hedef, sonuç doğrulanmadı) | likely_done (son eylem ekranı değiştirdi, Jev bitti
    # sanıyor ama emin değil) | needs_help | max_steps | error
    status: str
    steps: int
    reason: str
    log: list[str] = field(default_factory=list)
    title: str = ""
    location: str = ""
    changes: dict[str, list[str]] | None = None  # başa göre ekranda çıkan/kaybolan yazılar

    def report(self) -> str:
        head = f"reflex: {self.status} ({self.steps} adım) - {self.reason}\nekran: {self.title} | {self.location}"
        out = head + ("\nadımlar:\n" + "\n".join(self.log[-15:]) if self.log else "")
        c = self.changes or {}
        if c.get("appeared") or c.get("disappeared"):
            # Sonuç (hesap makinesindeki sayı) burada görünür; ana model okumak için ayrı tur harcamaz.
            out += ("\nekranda başa göre çıkan: " + "; ".join(c.get("appeared") or ["-"])
                    + "\nkaybolan: " + "; ".join(c.get("disappeared") or ["-"]))
        return out


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
        self._no_look = ""  # ekrana neden bakılamadığı (son söylenen)

    def _look(self) -> tuple[bytes, tuple[float, float], Any] | None:
        """Pencerenin Jev'e gidecek görüntüsü, Box kesirlerini asıl görüntüye çevirme çarpanı ve asıl görüntü;
        olmuyorsa None."""
        snap = getattr(self.obs, "snapshot", None)
        why = ("" if self.visual else "görsel istek bu çalışmada reddedildi") or \
              ("" if callable(snap) else "bu gözlemci ekran görüntüsü alamıyor") or \
              ("" if getattr(self.decider, "supports_images", False)
               else "görsel karar yok (TypeSafe anahtarı yok, [decider] jev_images kapalı ya da Jev görseli reddetti)")
        img = None
        if not why:
            try:
                img = snap()
            except Exception as e:  # noqa: BLE001 - görüntü alınamadı: görselsiz devam
                why = f"ekran görüntüsü alınamadı: {e}"
            else:
                why = "" if img is not None else "öndeki pencerenin görüntüsü alınamadı"
        if why:
            if why != self._no_look:  # her adımda aynı uyarıyı tekrarlama
                self.emit(f"ekrana bakılmıyor, sadece erişilebilirlik ağacıyla devam: {why}")
                self._no_look = why
            return None
        return prepare_image(img), padded_fraction(img.size), img

    def _zoom(self, goal: str, img: Any, box: tuple[float, float, float, float]) -> tuple[float, float, float] | None:
        """Kutunun çevresini asıl çözünürlükte kırpıp Jev'e yakından tekrar sor. Geniş ekranda (3440x1440) bütün
        pencere 1568 piksele küçülünce yazılar okunmuyor, Box kabaca doğru yeri gösterse de "var mı?" düşük
        kalıyordu. (var mı?, x, y) pencere kesirleriyle; olmazsa None."""
        l, t, r, b = box
        w, h = img.size
        hw, hh = max((r - l) * 1.5, ZOOM_SPAN[0] / 2), max((b - t) * 1.5, ZOOM_SPAN[1] / 2)
        cx, cy = min(max((l + r) / 2, hw), 1 - hw), min(max((t + b) / 2, hh), 1 - hh)
        x0, y0, x1, y1 = max(0.0, cx - hw), max(0.0, cy - hh), min(1.0, cx + hw), min(1.0, cy + hh)
        crop = img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
        try:
            d = self.decider.decide(
                {"goal": goal, "screen": ZOOM_REF, "note": "`screen` is a zoomed-in part of the window"},
                {"target_visible": Noul(TARGET_VISIBLE), "target": Box(TARGET)}, images={ZOOM_REF: prepare_image(crop)})
        except DeciderError as e:
            self.emit(f"yakından bakılamadı: {e}")
            return None
        zb = d["target"].value
        sx, sy = padded_fraction(crop.size)
        fx = x0 + (zb["left"] + zb["right"]) / 2 * sx * (x1 - x0)
        fy = y0 + (zb["top"] + zb["bottom"]) / 2 * sy * (y1 - y0)
        return d["target_visible"].value, min(1.0, fx), min(1.0, fy)

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
            tv = d.answers.get("target_visible")
            seen_on_screen = tv.value if tv is not None else None
            self.emit(f"[{step}] {nxt.value} (güven {nxt.confidence:.2f}, bitti? {done.value:.2f}"
                      + (f", ekranda var mı? {seen_on_screen:.2f}" if seen_on_screen is not None else "")
                      + f") {(time.monotonic() - t0) * 1000:.0f} ms via {d.engine}"
                      + (" [görsel]" if seen_on_screen is not None else " [metin]"))

            if done.value >= 0.85 or nxt.value == "done":
                return ReflexResult("done", step - 1, "hedefe ulaşıldı", log, ob.title, ob.location)
            if (seen_on_screen is not None and seen_on_screen >= ZOOM_MIN and nxt.value != VISUAL_KEY
                    and (nxt.value == "stuck" or nxt.confidence < self.min_conf)):
                # Seçeneklerde yok (ör. WebView2 penceresi ağaçta sadece "Document") ama Jev hedefi ekranda görüyor:
                # pes etmek yerine gördüğü yere tıkla.
                self.emit(f"[{step}] seçeneklerde uygun yok ama hedef ekranda görünüyor: ekrandaki yere tıklanıyor")
                nxt = replace(nxt, value=VISUAL_KEY)
            if nxt.value == "stuck":
                return ReflexResult("needs_help", step - 1, "uygun seçenek yok", log, ob.title, ob.location)
            if nxt.value == VISUAL_KEY:
                # Box her zaman bir kutu döndürür: "ekranda var mı?" düşükse kutuya güvenme, beyne bırak.
                seen = d["target_visible"].value
                b = d["target"].value
                sx, sy = look[1] if look else (1.0, 1.0)
                box = (min(1.0, b["left"] * sx), min(1.0, b["top"] * sy), min(1.0, b["right"] * sx),
                       min(1.0, b["bottom"] * sy))
                fx, fy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                close = self._zoom(goal, look[2], box) if look is not None and seen >= ZOOM_MIN else None
                if close is not None:
                    self.emit(f"[{step}] yakından: var mı? {close[0]:.2f}, kutu ({close[1]:.2f}, {close[2]:.2f}) "
                              f"(uzaktan {seen:.2f}, ({fx:.2f}, {fy:.2f}))")
                    if close[0] >= VISIBLE_MIN:
                        seen, fx, fy = close
                if seen < VISIBLE_MIN:
                    near = f", yakından {close[0]:.2f}" if close is not None else ""
                    return ReflexResult("needs_help", step - 1,
                                        f"ekranda tıklanacak şey bulunamadı (var mı? {seen:.2f}{near})",
                                        log, ob.title, ob.location)
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
                time.sleep(self.settle)
                if single_click_goal(goal):
                    return ReflexResult("clicked", step, f"ekranda hedef görülen yere tıklandı ({fx:.2f}, {fy:.2f})",
                                        log, ob.title, ob.location)
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
            {"role": "system", "content": "Return ONLY the exact text to type into the field. No quotes, no "
             "explanation. If the goal asks for several lines (a list, numbers one under another), put each on its "
             "own line."},
            {"role": "user", "content": f"Goal: {goal}\nField: {field_desc}\nText to type:"},
        ], temperature=0, max_tokens=400)
        return clean_typed(res.text)

    return write


def clean_typed(text: str) -> str:
    """Modelin yazılacak metni: tırnak ve kod bloğu çitleri atılır, satırlar korunur. Sadece ilk satır alınıyordu:
    "1'den 10'a alt alta yaz" hedefinde 20 adım boyunca "1, Enter" yazıldı."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip().strip('"').strip("'")
