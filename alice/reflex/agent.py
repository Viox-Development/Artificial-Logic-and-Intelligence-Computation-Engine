"""Ekran ajanı: Jev her adımda ne yapılacağını öğe listesinden seçer, kod uygular ve sonucu doğrular.

TypeSafe'in kendi rehberi ve Jev'le bilgisayar/oyun oynayan projelerden (typesafe-computer-use, CUA-JEV,
jev-factorio-agent, tsai-civ2) çıkan düzen:

- ekran deterministik okunur (perceive.py): numaralı, konumlu, tekrarsız öğeler;
- Jev'e tek istekte ayrı ayrı sorulur (spekülatif): eylemin türü, hangi öğe, hangi tuş, nereye bırakılacak,
  hedef bitti mi, hedefin üzerinde çalışılacak şey listede yok mu. Tür ve öğe ayrı sorulur: anlamı örtüşen
  seçenekler (aynı öğe için "tıkla" ve "çift tıkla") oyu bölüp güveni düşürüyordu;
- hedef İngilizceye çevrilir (Jev en iyi İngilizcede), beyin modelinin notları (oyunun kuralları, strateji)
  durumun içinde gider;
- geçmiş, koordinat değil okunur cümledir: "clicked button 'Kaydet' (top-right) -> screen changed";
- her eylemden sonra ekranın değişip değişmediğine bakılır; işe yaramayan eylem tekrar tekrar denenmez;
- listede olmayan bir hedef (ikon, oyun tahtası) için Jev'in görsel sorusu (var mı? + Box) yedek olarak kalır;
- her adım ~/.alice/reflex-trace.jsonl'a yazılır: neden başarısız olduğu görülebilsin.

Metin üretmek Jev'in işi değil: yazılacak metin `inputs`tan ya da hızlı modelden (text_writer) gelir.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from alice.decider import Box, Choice, DeciderError, Noul, padded_fraction, prepare_image
from alice.reflex.loop import ReflexResult, single_click_goal
from alice.reflex.observers import KEYS, WAIT_SECONDS
from alice.reflex.perceive import (Item, Screen, changed_crops, changed_places, image_changed, image_key, place,
                                   screen_changes)

SCREEN_REF = "alice-screen-9d2f4b71"
ZOOM_REF = "alice-zoom-5e8a03c6"
START_REF = "alice-start-3c71e0a9"  # oyun turunun başındaki ekran
START_CROP_REF = "alice-startcrop-6b2d94e0"  # değişen bölge, turun başında
NOW_CROP_REF = "alice-nowcrop-a81f5c37"  # aynı bölge, şimdi

ACTION_CRITERIA = {
    "click": "Click one item (a button, link, tab, menu entry, cell or piece of text).",
    "double_click": "Double-click one item, for example to open a file, folder or game unit.",
    "right_click": "Right-click one item to open its context menu.",
    "type_text": "Type text into one input field or editable area.",
    "press_key": "Press a keyboard key or shortcut without choosing an item, for example Enter to confirm, "
                 "Escape to close a dialog, or an arrow key.",
    "drag": "Drag one item and drop it onto another place, for example moving a card, unit, file or slider.",
    "scroll_down": "Scroll down because what `goal` needs is probably further down.",
    "scroll_up": "Scroll up because what `goal` needs is probably further up.",
    "wait": "Wait because the screen is loading, animating, or it is not the user's turn yet.",
}
VISUAL_ACTION = "click_unlisted"
VISUAL_DRAG = "drag_unlisted"
VISUAL_DRAG_CRITERION = ("Drag something visible in `screen` that is not in `items`, such as a card, a game piece or a "
                         "unit, and drop it somewhere else in `screen`.")
VISUAL_CRITERION = ("Click something visible in `screen` that is not in `items`, such as an icon, a picture, "
                    "a map tile or a game piece.")
DONE_MIN = 0.85
CHANGED_MIN = 0.75  # görsel kıyas (changed_as_asked) gözlenebilir tek yargı: TypeSafe'in 0.7 'evet' eşiğine yakın
LIKELY_DONE_MIN = 0.6  # sıradaki eylemden emin değilken, son eylem ekranı değiştirdiyse
SCREEN_RETRY = 0.5  # sn: ekran okunamazsa bu kadar bekleyip bir kez daha
LOOP_MAX = 2  # aynı ekranda aynı eylem bundan fazla yapılmaz (A-B-A-B gidip gelme)
MISSING_MIN = 0.7
VISIBLE_MIN = 0.7
ZOOM_MIN = 0.4
KIND_MIN = 0.4  # eylem türünde bundan az eminse beyne bırak
TRACE_MAX = 2_000_000  # bayt: iz dosyası bundan büyükse yarısı silinir
_KEY_RX = re.compile(r"\b((?:ctrl|alt|shift|win)\+[\w+]+|f\d{1,2})\b", re.I)


def _pick(answers: dict[str, Any], question: str, pool: list[Item]) -> Item | None:
    """Jev'in seçtiği öğe; tek seçenek olduğu için soru sorulmadıysa o tek öğe."""
    if question in answers:
        return next((i for i in pool if i.key == answers[question].value), None)
    return pool[0] if len(pool) == 1 else None


def already_has(value: str, text: str) -> bool:
    """Alan yazılacak metni zaten içeriyor mu? Değer okunurken 80 karakterde kesiliyor."""
    v, t = (value or "").strip(), (text or "").strip()
    return bool(v and t) and (v == t or (len(v) >= 78 and t.startswith(v)))


def goal_keys(goal: str) -> list[str]:
    """Hedefte yazan kısayollar (ör. 'ctrl+shift+n') da seçeneklere girsin."""
    return [k.lower() for k in _KEY_RX.findall(goal)]


class ScreenAgent:
    def __init__(self, observer: Any, decider: Any, text_writer: Callable[[str, str], str] | None = None,
                 translate: Callable[[str], str] | None = None, emit: Callable[[str], None] = lambda s: None,
                 settle: float = 0.4, trace_path: Any = None):
        self.obs = observer
        self.decider = decider
        self.text_writer = text_writer
        self.translate = translate
        self.emit = emit
        self.settle = settle
        self.trace_path = trace_path
        self.visual = True
        self.last: Screen | None = None  # son okunan ekran (oyun turunun özetinde yeni çıkan yazılar için)
        # Oyun oturumu açar: Jev'e turun başından beri değişenler gider, her eylemin kanıtı (ne çıktı, ne kayboldu)
        # yansıtma için self.evidence'ta tutulur.
        self.track_changes = False
        # Oyunda hamle: ekranı değiştiren ilk eylemden sonra dur. Solitaire'de ilk tıklama 4♣'yi taşımıştı ama Jev
        # bunu tanımayıp tıklamaya devam etti; doğrulamayı tahtayı okuyan model yapar (Voyager, Cradle).
        self.stop_after_change = False
        self.evidence: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ yardımcılar
    def _english(self, goal: str) -> str:
        if self.translate is None:
            return goal
        try:
            out = (self.translate(goal) or "").strip()
            return out or goal
        except Exception:  # noqa: BLE001 - çeviri olmazsa asıl hedefle
            return goal

    def _can_see(self, scr: Screen) -> bool:
        return (self.visual and scr.image is not None and callable(getattr(self.obs, "click_at", None))
                and bool(getattr(self.decider, "supports_images", False)))

    def _trace(self, row: dict[str, Any]) -> None:
        if self.trace_path is None:
            return
        try:
            p = self.trace_path
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > TRACE_MAX:
                lines = p.read_text(encoding="utf-8").splitlines()
                p.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _text_for(self, item: Item, goal: str, inputs: dict[str, str]) -> str:
        label = item.label.lower()
        for k, v in inputs.items():
            if k.lower() in label:
                return v
        if len(inputs) == 1:
            return next(iter(inputs.values()))
        if self.text_writer is None:
            raise DeciderError("yazılacak metin yok (inputs ver)")
        return self.text_writer(goal, item.describe())

    def _questions(self, scr: Screen, keys: list[str], see: bool,
                   compare: tuple[str, str] | None = None) -> dict[str, Any]:
        items = {i.key: i.describe() for i in scr.items}
        kinds = dict(ACTION_CRITERIA)
        if not any(i.editable for i in scr.items) and not any(i.source == "ocr" for i in scr.items):
            kinds.pop("type_text")  # yazılacak alan yok
        if len(scr.items) < 2:
            kinds.pop("drag")
        if see:
            kinds[VISUAL_ACTION] = VISUAL_CRITERION
            if callable(getattr(self.obs, "drag_at", None)):
                kinds[VISUAL_DRAG] = VISUAL_DRAG_CRITERION
        q: dict[str, Any] = {
            "action": Choice("Which kind of action should be done next to make progress toward `goal`?", kinds),
            "done": Noul("Has `goal` already been fully achieved?", {
                "true": "`items` and `window` show the end state `goal` asks for, or `recent_actions` show that its "
                        "last required action already worked."
                        + (" `changes_since_start` lists what appeared and disappeared on screen since the start, "
                           "and `screen_at_start` shows the screen before the first action; the change `goal` asks "
                           "for is visible in them." if self.track_changes else ""),
                "false": "Something `goal` asks for has not happened yet."}),
            "missing": Noul("Is the thing `goal` needs to act on next absent from `items`?", {
                "true": "No entry in `items` is the control, text or object to act on next.",
                "false": "One of the entries in `items` is what should be acted on next."}),
            "key": Choice("Which key or shortcut should be pressed next for `goal`?",
                          {k: f"Press {k}" for k in keys}),
        }
        # Choice en az iki seçenek ister; tek seçenek varsa sorulmaz, kod onu kullanır (_pick).
        if len(items) > 1:
            q["item"] = Choice("Which item in `items` should the next action be done on, to make progress toward "
                               "`goal`?", items)
            q["drop"] = Choice("If an item is dragged next, onto which item in `items` should it be dropped?", items)
        fields = {i.key: i.describe() for i in scr.items if i.editable}
        if len(fields) > 1:
            q["field"] = Choice("If text is typed next, into which input field should it be typed for `goal`?", fields)
        if see:
            q["target_visible"] = Noul("Is the thing `goal` needs to click next visible in `screen`?")
            q["target"] = Box("the thing in `screen` that should be clicked next to work toward `goal`")
            if VISUAL_DRAG in kinds:
                # Kart, taş, birim yazı değil: listede yoklar, sürüklemek için de ekrandan bulunmaları gerekiyor.
                q["drag_visible"] = Noul("Is the thing `goal` needs to drag next visible in `screen`?")
                q["drag_source"] = Box("the thing in `screen` that should be dragged next to work toward `goal`")
                q["drop_place"] = Box("the place in `screen` where the dragged thing should be dropped for `goal`")
        if see and compare:
            # "Tamamen başarıldı mı?" soyut kaldı (Solitaire'de kart çekildiği halde 0.1). Gözlenebilir tek yargı:
            # istenen değişiklik önceki karede yok, şimdikinde var mı? Değişen bölge varsa onun büyük kırpıntıları
            # kıyaslanır (EFR): küçültülmüş tam ekranda kartın rakamı birkaç piksel kalıyordu.
            before, after = compare
            q["changed_as_asked"] = Noul(f"Does `{after}` show the change `goal` asks for, which `{before}` "
                                         "does not show?", {
                "true": f"Something `goal` asks to happen, such as a card drawn, a piece moved or a screen opened, is "
                        f"visible in `{after}` but not in `{before}`.",
                "false": f"Everything `goal` is about looks the same in `{after}` as in `{before}`, or the "
                         "difference is something else."})
        return q

    # ------------------------------------------------------------------ döngü
    def run(self, goal: str, max_steps: int = 25, inputs: dict[str, str] | None = None,
            notes: str | Callable[[Screen], str] = "") -> ReflexResult:
        """notes: sabit metin ya da her adımda ekrana göre seçen bir fonksiyon (oyunda: ekrandakine uyan dersler)."""
        res = self._run(goal, max_steps, inputs or {}, notes)
        # Sonuç satırı: "clicked"/"done" doğru anda mı geldi, izden görülsün.
        self._trace({"ts": time.time(), "goal": goal, "result": res.status, "steps": res.steps, "reason": res.reason,
                     "window": res.title})
        return res

    def _run(self, goal: str, max_steps: int, inputs: dict[str, str],
             notes: str | Callable[[Screen], str]) -> ReflexResult:
        en = self._english(goal)
        if en != goal:
            self.emit(f"hedef: {en}")
        keys = list(dict.fromkeys(list(KEYS) + goal_keys(goal)))
        log: list[str] = []
        history: list[str] = []  # Jev'e giden okunur geçmiş
        tried: dict[str, int] = {}  # eylem -> ekran değiştirmeden kaç kez denendi
        visits: dict[tuple[str, ...], int] = {}  # (ekran imzası, eylem) -> kaç kez yapıldı
        scr = Screen("", "")
        last_sig = ""
        start_items: list[Item] | None = None
        prev_items: list[Item] = []
        prev_image: Any = None
        start_image: Any = None
        self.evidence = []
        for step in range(1, max_steps + 1):
            try:
                scr = self.obs.screen(goal)
            except Exception:  # noqa: BLE001 - UIA bazen geçici zaman aşımı veriyor (0x80131505): bir kez daha dene
                time.sleep(SCREEN_RETRY)
                try:
                    scr = self.obs.screen(goal)
                except Exception as e:  # noqa: BLE001
                    return ReflexResult("error", step - 1, f"ekran okunamadı: {e}", log)
            self.last = scr
            sig = scr.signature()
            if history and last_sig and not history[-1].endswith(("changed", "on screen")):
                # Son eylem işe yaradı mı? Jev bunu okunur geçmişte görür; aynı boşa eylem üçüncü kez yapılmaz.
                did = history[-1]
                changed = sig != last_sig or image_changed(prev_image, scr.image)
                if changed:
                    history[-1] += " -> screen changed"
                    log[-1] += " (ekran değişti)"  # beyin modeli de görsün: ör. Ctrl+S Kaydet penceresini açtı
                else:
                    history[-1] += " -> nothing changed on screen"
                    log[-1] += " (ekran değişmedi)"
                if self.track_changes:
                    # Yansıtmanın kanıtı: bu eylemden sonra ne çıktı, ne kayboldu (kod biliyor, model uydurmasın).
                    self.evidence.append({"action": history[-1], "changed": changed,
                                          **screen_changes(prev_items, scr.items),
                                          "changed_places": changed_places(prev_image, scr.image) if changed else []})
                if changed and self.stop_after_change:
                    return ReflexResult("acted", step - 1, "eylem ekranı değiştirdi; istenen olup olmadığı ayrıca "
                                        "doğrulanacak", log, scr.title, scr.location)
                if not changed:
                    tried[did] = tried.get(did, 0) + 1
                    if tried[did] >= 2:
                        return ReflexResult("needs_help", step - 1, f"aynı eylem iki kez ekranı değiştirmedi: {did}",
                                            log, scr.title, scr.location)
            last_sig = sig
            prev_items, prev_image = scr.items, scr.image
            if start_items is None:
                start_items, start_image = scr.items, scr.image
            see = self._can_see(scr)
            state: dict[str, Any] = {"goal": en, "window": scr.title, "items": {i.key: i.describe() for i in scr.items},
                                     "recent_actions": history[-6:] or ["none yet"]}
            if self.track_changes and history:
                # "Bitti mi?" için kodun bildiği gerçek: turun başından beri ekranda ne değişti. Tur sayacı, yıl, kaynak
                # sayıları her oyunda burada görünür; oyuna özel kural yazmak gerekmez.
                state["changes_since_start"] = screen_changes(start_items, scr.items)
            note_text = notes(scr) if callable(notes) else notes
            if note_text:
                state["notes"] = note_text
            if en != goal:
                state["goal_original_language"] = goal
            images = None
            compare: tuple[str, str] | None = None
            if see:
                state["screen"] = SCREEN_REF
                images = {SCREEN_REF: prepare_image(scr.image)}
                if self.track_changes and history and start_image is not None:
                    # Önce/sonra (Cradle, EFR): yazısız değişimi (kart açıldı, birim yürüdü) Jev görerek kıyaslasın.
                    state["screen_at_start"] = START_REF
                    images[START_REF] = prepare_image(start_image)
                    compare = ("screen_at_start", "screen")
                    crops = changed_crops(start_image, scr.image)
                    if crops is not None:
                        state["changed_part_at_start"], state["changed_part_now"] = START_CROP_REF, NOW_CROP_REF
                        images[START_CROP_REF], images[NOW_CROP_REF] = prepare_image(crops[0]), prepare_image(crops[1])
                        compare = ("changed_part_at_start", "changed_part_now")
            t0 = time.monotonic()
            try:
                d = self.decider.decide(state, self._questions(scr, keys, see, compare),
                                        images=images)
            except DeciderError as e:
                if images is None:
                    return ReflexResult("error", step - 1, f"karar motoru hatası: {e}", log, scr.title, scr.location)
                self.visual = False  # görsel istek reddedildi: bu çalışmada görselsiz devam
                self.emit(f"görsel karar olmadı, görselsiz devam: {e}")
                see = False
                state.pop("screen", None)
                for k in ("screen_at_start", "changed_part_at_start", "changed_part_now"):
                    state.pop(k, None)
                try:
                    d = self.decider.decide(state, self._questions(scr, keys, False))
                except DeciderError as e2:
                    return ReflexResult("error", step - 1, f"karar motoru hatası: {e2}", log, scr.title, scr.location)
            ms = (time.monotonic() - t0) * 1000
            a = d.answers
            act, done, missing = a["action"], a["done"].value, a["missing"].value
            if "changed_as_asked" in a:  # görerek kıyas: istenen değişiklik oldu mu (kod iki yargıdan birini yeter sayar)
                done = max(done, a["changed_as_asked"].value)
            item = _pick(a, "item", scr.items)
            self.emit(f"[{step}] {act.value} {item.describe() if item and act.value != 'press_key' else ''}"
                      f"{a['key'].value if act.value == 'press_key' else ''} (güven {act.confidence:.2f}, "
                      f"bitti? {done:.2f}, listede yok? {missing:.2f}) {ms:.0f} ms, {len(scr.items)} öğe"
                      + (" [görsel]" if see else ""))
            self._trace({"ts": time.time(), "goal": goal, "goal_en": en, "step": step, "window": scr.title,
                         "items": len(scr.items), "action": act.value, "action_conf": round(act.confidence, 3),
                         "item": item.describe() if item else None,
                         "item_conf": round(a["item"].confidence, 3) if "item" in a else None,
                         "key": a["key"].value if act.value == "press_key" else None, "done": round(done, 3),
                         "missing": round(missing, 3), "ms": round(ms), "visual": see,
                         "changed_as_asked": round(a["changed_as_asked"].value, 3) if "changed_as_asked" in a else None,
                         "list": [f"{i.key} {i.describe()}" + (f" @{tuple(round(v, 3) for v in i.rect)}" if i.rect else "")
                                  for i in scr.items]})
            seen_change = a["changed_as_asked"].value if "changed_as_asked" in a else 0.0
            if done >= DONE_MIN or seen_change >= CHANGED_MIN:
                return ReflexResult("done", step - 1, "hedefe ulaşıldı", log, scr.title, scr.location)
            choice = act.value
            if missing >= MISSING_MIN and choice in ("click", "double_click", "right_click", "type_text", "drag"):
                choice = (VISUAL_DRAG if choice == "drag" and "drag_source" in a else VISUAL_ACTION) if see else ""
            if choice == VISUAL_DRAG:
                res = self._visual_drag(step, scr, a, log, history)
                if isinstance(res, ReflexResult):
                    return res
                time.sleep(self.settle)
                continue
            if choice == VISUAL_ACTION:
                res = self._visual_click(step, scr, en, a, log, history)
                if isinstance(res, ReflexResult):
                    if res.status == "needs_help" and done >= LIKELY_DONE_MIN and history                             and history[-1].endswith("screen changed"):
                        # Kart çekildi (görsel kıyas 0.8), Jev sıradaki tıklanacak şeyi bulamadı: "yapılamadı" değil.
                        return ReflexResult("likely_done", step - 1, f"son eylem ekranı değiştirdi, büyük ihtimalle "
                                            f"bitti (bitti? {done:.2f})", log, scr.title, scr.location)
                    return res
                if single_click_goal(goal):
                    return ReflexResult("clicked", step, "ekranda hedef görülen yere tıklandı", log, scr.title,
                                        scr.location)
                time.sleep(self.settle)
                continue
            if not choice:
                return ReflexResult("needs_help", step - 1, "hedefin üzerinde çalışılacak şey ekranda bulunamadı "
                                    f"(listede yok? {missing:.2f})", log, scr.title, scr.location)
            if (act.confidence < KIND_MIN or choice == "wait") and done >= LIKELY_DONE_MIN and history \
                    and history[-1].endswith("screen changed"):
                # Sürükleme işe yaradı (dosya klasöre girdi) ama "bitti?" 0.79'da kaldı ve sıradaki eylemden emin
                # değil: "yapılamadı" demek yanlış olurdu.
                return ReflexResult("likely_done", step - 1, f"son eylem ekranı değiştirdi, büyük ihtimalle bitti "
                                    f"(bitti? {done:.2f})", log, scr.title, scr.location)
            if act.confidence < KIND_MIN:
                return ReflexResult("needs_help", step - 1, f"ne yapılacağından emin değil (güven {act.confidence:.2f}"
                                    f", en olası: {choice})", log, scr.title, scr.location)
            if choice == "type_text":
                # Yazılacak yer ayrı sorulur: "item" sorusunda Jev Gezgin'in sol ağacındaki 'Desktop'u seçip oraya
                # "yazdırıyordu" (tıklandı, Masaüstü açıldı).
                item = _pick(a, "field", [i for i in scr.items if i.editable]) or item
            needs_item = choice in ("click", "double_click", "right_click", "type_text", "drag")
            if needs_item and item is None:
                return ReflexResult("needs_help", step - 1, "ekranda seçilecek öğe yok", log, scr.title, scr.location)
            drop = _pick(a, "drop", scr.items) if choice == "drag" else None
            # Aynı ekranda aynı eylem üçüncü kez: döngü. Unciv'de 'Pick a tech!' ile 'Close' arasında 15 adım gidip
            # geldi; her tıklama ekranı değiştirdiği için "ekran değişmedi" koruması yakalamıyordu.
            move = (sig, image_key(scr.image), choice, item.describe(False) if needs_item and item else "",
                    drop.describe(False) if drop else "", str(a["key"].value) if choice == "press_key" else "")
            visits[move] = visits.get(move, 0) + 1
            if visits[move] > LOOP_MAX:
                return ReflexResult("needs_help", step - 1, f"döngüye girdi: aynı ekranda aynı eylem {LOOP_MAX + 1}. "
                                    f"kez ({choice} {move[3]})".strip(), log, scr.title, scr.location)
            text = key = ""
            try:
                if choice == "type_text":
                    text = self._text_for(item, en, inputs)
                    if already_has(item.value, text):
                        # Aynı yolu adres çubuğuna tekrar tekrar yazıyordu. Eylem yapılmaz; ekran değişmeyeceği için
                        # tekrar sayacına da girer.
                        history.append(f"did not type: {item.describe(False)} already contains '{text[:40]}'")
                        log.append(f"{step}. yazılmadı: alan zaten '{text[:40]}' içeriyor")
                        continue
                if choice == "press_key":
                    key = str(a["key"].value)
                self.obs.act(choice, item if needs_item else None, text=text, keys=key, drop=drop)
            except Exception as e:  # noqa: BLE001
                log.append(f"{step}. {choice} -> HATA {e}")
                history.append(f"{choice} failed: {str(e)[:120]}")
                time.sleep(self.settle)
                continue
            said = {"click": "clicked", "double_click": "double-clicked", "right_click": "right-clicked",
                    "type_text": f"typed '{text[:40]}' into", "drag": "dragged", "press_key": f"pressed {key}",
                    "scroll_down": "scrolled down", "scroll_up": "scrolled up", "wait": f"waited {WAIT_SECONDS:g}s"}
            what = item.describe(False) if needs_item and item else ""
            if choice == "drag" and drop is not None:
                what += f" onto {drop.describe(False)}"
            history.append(f"{said.get(choice, choice)} {what}".strip())
            log.append(f"{step}. {history[-1]}")
            time.sleep(self.settle)
            if choice == "click" and single_click_goal(goal) and item is not None:
                after = self._safe_screen(goal)
                if after is not None and (after.signature() != sig or image_changed(scr.image, after.image)):
                    log[-1] += " (ekran değişti)"
                    self.last = after
                    if self.track_changes:
                        self.evidence.append({"action": history[-1], "changed": True,
                                              **screen_changes(scr.items, after.items),
                                              "changed_places": changed_places(scr.image, after.image)})
                    return ReflexResult("clicked", step, f"tıklandı: {item.describe(False)}", log, after.title,
                                        after.location)
        return ReflexResult("max_steps", max_steps, "adım limiti doldu", log, scr.title, scr.location)

    def _safe_screen(self, goal: str) -> Screen | None:
        try:
            return self.obs.screen(goal)
        except Exception:  # noqa: BLE001
            return None

    def _visual_click(self, step: int, scr: Screen, goal: str, a: dict[str, Any], log: list[str],
                      history: list[str]) -> ReflexResult | None:
        """Listede olmayan hedef: Jev'in görsel cevabı (var mı? + Box), emin değilse yakından tekrar."""
        seen = a["target_visible"].value if "target_visible" in a else 0.0
        if "target" not in a:
            return ReflexResult("needs_help", step - 1, "hedef listede yok ve ekrana bakılamıyor", log, scr.title,
                                scr.location)
        b = a["target"].value
        sx, sy = padded_fraction(scr.image.size)
        box = (min(1.0, b["left"] * sx), min(1.0, b["top"] * sy), min(1.0, b["right"] * sx), min(1.0, b["bottom"] * sy))
        fx, fy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if seen >= ZOOM_MIN:
            close = self._zoom(goal, scr.image, box)
            if close is not None and close[0] >= VISIBLE_MIN:
                seen, fx, fy = close
        if seen < VISIBLE_MIN:
            return ReflexResult("needs_help", step - 1, f"ekranda tıklanacak şey bulunamadı (var mı? {seen:.2f})",
                                log, scr.title, scr.location)
        try:
            self.obs.click_at(fx, fy)
        except Exception as e:  # noqa: BLE001
            log.append(f"{step}. ekranda tıklama -> HATA {e}")
            history.append(f"click on screen failed: {str(e)[:120]}")
            return None
        log.append(f"{step}. ekranda görülen yere tıklandı ({fx:.2f}, {fy:.2f}) var mı? {seen:.2f}")
        history.append(f"clicked something visible on screen at {place(fx, fy)}")
        return None

    def _visual_drag(self, step: int, scr: Screen, a: dict[str, Any], log: list[str],
                     history: list[str]) -> ReflexResult | None:
        """Listede olmayan bir şeyi (kart, taş, birim) ekranda görülen yerden alıp görülen yere bırak."""
        seen = a["drag_visible"].value if "drag_visible" in a else 0.0
        if seen < VISIBLE_MIN or "drop_place" not in a:
            return ReflexResult("needs_help", step - 1, f"sürüklenecek şey ekranda bulunamadı (var mı? {seen:.2f})",
                                log, scr.title, scr.location)
        sx, sy = padded_fraction(scr.image.size)

        def center(b: dict[str, float]) -> tuple[float, float]:
            return min(1.0, (b["left"] + b["right"]) / 2 * sx), min(1.0, (b["top"] + b["bottom"]) / 2 * sy)

        (fx, fy), (tx, ty) = center(a["drag_source"].value), center(a["drop_place"].value)
        try:
            self.obs.drag_at(fx, fy, tx, ty)
        except Exception as e:  # noqa: BLE001
            log.append(f"{step}. ekranda sürükleme -> HATA {e}")
            history.append(f"drag on screen failed: {str(e)[:120]}")
            return None
        log.append(f"{step}. ekranda görülen şey sürüklendi ({fx:.2f}, {fy:.2f}) -> ({tx:.2f}, {ty:.2f})")
        history.append(f"dragged something visible on screen from {place(fx, fy)} to {place(tx, ty)}")
        return None

    def _zoom(self, goal: str, img: Any, box: tuple[float, float, float, float]) -> tuple[float, float, float] | None:
        l, t, r, b = box
        w, h = img.size
        hw, hh = max((r - l) * 1.5, 0.15), max((b - t) * 1.5, 0.2)
        cx, cy = min(max((l + r) / 2, hw), 1 - hw), min(max((t + b) / 2, hh), 1 - hh)
        x0, y0, x1, y1 = max(0.0, cx - hw), max(0.0, cy - hh), min(1.0, cx + hw), min(1.0, cy + hh)
        crop = img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
        try:
            d = self.decider.decide({"goal": goal, "screen": ZOOM_REF, "note": "`screen` is a zoomed-in part of the window"},
                                    {"target_visible": Noul("Is the thing `goal` needs to click next visible in `screen`?"),
                                     "target": Box("the thing in `screen` that should be clicked next to work toward `goal`")},
                                    images={ZOOM_REF: prepare_image(crop)})
        except DeciderError:
            return None
        zb = d["target"].value
        sx, sy = padded_fraction(crop.size)
        fx = x0 + (zb["left"] + zb["right"]) / 2 * sx * (x1 - x0)
        fy = y0 + (zb["top"] + zb["bottom"]) / 2 * sy * (y1 - y0)
        return d["target_visible"].value, min(1.0, fx), min(1.0, fy)


def make_translator(router) -> Callable[[str], str]:
    """Hedefi İngilizceye çevir (Jev en iyi İngilizcede). Ekranda görünen adlar ve tırnak içi metin aynen kalır."""

    def translate(goal: str) -> str:
        if goal.isascii():
            return goal
        res = router.chat("fast", [
            {"role": "system", "content": "Translate the user's instruction for a computer-use agent into English. "
             "Keep names of buttons, menus, files, folders, projects, games and any quoted text exactly as written "
             "(do not translate them, even when a name is an ordinary word, e.g. a folder called 'hedef' stays "
             "'hedef'). Return only the translation."},
            {"role": "user", "content": goal},
        ], temperature=0, max_tokens=200)
        return res.text.strip().strip('"')

    return translate
