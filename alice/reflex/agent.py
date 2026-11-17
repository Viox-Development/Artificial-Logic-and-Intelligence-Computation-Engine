"""Screen agent: at each step Jev chooses what to do from the item list, the code applies it and verifies the result.

The layout comes from TypeSafe's own guide and from projects where Jev plays computers and games
(typesafe-computer-use, CUA-JEV, jev-factorio-agent, tsai-civ2):

- the screen is read deterministically (perceive.py): numbered, positioned, non-duplicate items;
- Jev is asked separately, one question per request (speculatively): the kind of action, which item, which key,
  where to drop, whether the goal is done, and whether what to work on is missing from the list. Kind and item are
  asked separately: options with overlapping meanings ("click" and "double-click" for the same item) split the vote
  and lowered confidence;
- the goal is translated to English (Jev works best in English); the brain model's notes (game rules, strategy)
  go along in the state;
- history is a readable sentence, not coordinates: "clicked button 'Kaydet' (top-right) -> screen changed";
- after every action it checks whether the screen changed; an action that did nothing is not retried again and again;
- for a target that is not in the list (an icon, a game board), Jev's visual question (is it there? + Box) stays
  as a fallback;
- every step is written to ~/.alice/reflex-trace.jsonl so the reason for a failure can be seen.

Generating text is not Jev's job: the text to write comes from `inputs` or from the fast model (text_writer).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from alice.decider import Box, Choice, DeciderError, Noul, padded_fraction, prepare_image
from alice.reflex.loop import ReflexResult, login_text, single_click_goal
from alice.reflex.observers import KEYS, WAIT_SECONDS
from alice.reflex.perceive import (Item, Screen, changed_crops, changed_places, image_changed, image_key, place,
                                   screen_changes)

SCREEN_REF = "alice-screen-9d2f4b71"
ZOOM_REF = "alice-zoom-5e8a03c6"
START_REF = "alice-start-3c71e0a9"  # screen at the start of the game turn
START_CROP_REF = "alice-startcrop-6b2d94e0"  # the changed region, at the start of the turn
NOW_CROP_REF = "alice-nowcrop-a81f5c37"  # the same region, now

ACTION_CRITERIA = {
    "click": "Click one item (a button, link, tab, menu entry, cell or piece of text).",
    "double_click": "Double-click one item, for example to open a file, folder or game unit.",
    "right_click": "Right-click one item to open its context menu.",
    "type_text": "Type text into one input field or editable area.",
    "press_key": "Press a keyboard key or shortcut without choosing an item, for example Enter to confirm, "
                 "Escape to close a dialog, or an arrow key.",
    "type_keys": "Type text with the keyboard into the window itself without choosing a field, for example a "
                 "calculation into a calculator, a command into a terminal or a word in a game.",
    "hold_key": "Hold a key or key combination down for a moment, for example to keep moving or running in a game.",
    "draw_across": "Drag the mouse across one area, such as a canvas, picture or map, from its upper-left part to its "
                   "lower-right part, for example to draw a shape or select a region.",
    "drag": "Drag one item and drop it onto another place, for example moving a card, unit, file or slider.",
    "scroll_down": "Scroll down because what `goal` needs is probably further down.",
    "scroll_up": "Scroll up because what `goal` needs is probably further up.",
    "wait": "Wait because the screen is loading, animating, or it is not the user's turn yet.",
}
VISUAL_ACTION = "click_unlisted"
VISUAL_DRAG = "drag_unlisted"


def grab_box(verb: str) -> str:
    """Where the box should point: the visible, uncovered part of the thing. In overlapping cards, the center of
    Q♠'s box landed on the J♥ lying on top of it; since J♥ cannot be moved alone, nothing happened."""
    return (f"the visible, uncovered part of the thing in `screen` that should be {verb} next to work toward `goal`, "
            f"where it can be {verb} without touching anything lying on top of it")
VISUAL_DRAG_CRITERION = ("Drag something visible in `screen` that is not in `items`, such as a card, a game piece or a "
                         "unit, and drop it somewhere else in `screen`.")
VISUAL_CRITERION = ("Click something visible in `screen` that is not in `items`, such as an icon, a picture, "
                    "a map tile or a game piece.")
DONE_MIN = 0.85
CHANGED_MIN = 0.75  # visual comparison (changed_as_asked), the one observable verdict: near TypeSafe's 0.7 'yes'
LIKELY_DONE_MIN = 0.6  # when unsure of the next action, if the last action changed the screen
SCREEN_RETRY = 0.5  # seconds: if the screen cannot be read, wait this long and try once more
LOOP_MAX = 2  # the same action is not done more than this on the same screen (no A-B-A-B back and forth)
ITEM_SURE = 0.6  # if the best candidate in item selection is below this, take a second look
SECOND_LOOK_MIN = 0.15  # the second candidate must be at least this likely (otherwise no second look is needed)
SECOND_LOOK_TOP = 4  # number of candidates compared in the second look
MAX_CONSULTS = 2  # at most this many internal consultations in one reflex call
DRAW_MIN_AREA = 0.04  # draw_across: if the item is smaller than this share of the window it is not an area (a
# button), so the area Jev saw is used
CONSULT_STEPS = 6  # max steps for a step that comes from a consultation
# Buttons that may be irreversible (only the start of the word is matched, for Turkish suffixes): gönder (send),
# öde (pay), sil (delete), satın al (buy), onayla (confirm)...
RISKY_RX = re.compile(r"(?<![\w])(gönder|send|sil|delete|remove|kaldır|satın|buy|purchase|öde|pay|sipariş|order|"
                      r"checkout|yayınla|publish|post|paylaş|share|kabul|accept|agree|onayla|confirm|abone|"
                      r"subscribe|uninstall|format|boşalt|empty trash)", re.I)
MISSING_MIN = 0.7
VISIBLE_MIN = 0.7
ZOOM_MIN = 0.4
KIND_MIN = 0.4  # if less sure than this about the action kind, leave it to the brain
ITEM_CLEAR = 0.9  # ...but if the item is this clear and the most likely kind is a click, click
KIND_FLOOR = 0.3  # (a click must be at least this likely)
TRACE_MAX = 2_000_000  # bytes: if the trace file is larger than this, half of it is deleted
_KEY_RX = re.compile(r"\b((?:ctrl|alt|shift|win)\+[\w+]+|f\d{1,2})\b", re.I)


def _pick(answers: dict[str, Any], question: str, pool: list[Item]) -> Item | None:
    """The item Jev chose; if there was only one option, so no question was asked, that single item."""
    if question in answers:
        return next((i for i in pool if i.key == answers[question].value), None)
    return pool[0] if len(pool) == 1 else None


def wrong_field(focused: str, text: str) -> bool:
    """Is multi-line text going into a single-line field (Edit, ComboBox)? Line breaks turn into spaces."""
    return "\n" in text and focused.startswith(("Edit ", "ComboBox "))


def already_has(value: str, text: str) -> bool:
    """Does the field already contain the text to be typed? The value is cut off at 80 characters when read."""
    v, t = (value or "").strip(), (text or "").strip()
    return bool(v and t) and (v == t or (len(v) >= 78 and t.startswith(v)))


# The goal explicitly forbids touching the screen ("hiçbir düğmeye basma" = press no button, "do not click anything").
# Saying only "değiştirme" (don't change) is not enough: a click may be needed to open the page.
NO_TOUCH_RX = re.compile(
    r"(hi[çc]bir|herhangi bir)\s+(şeye|seye|düğmeye|dugmeye|kontrole|yere|tuşa|tusa)\s*(de\s+)?(bas|tıkla|tikla|dokun)|"
    r"\b(tıklamadan|tiklamadan|dokunmadan)\b|"
    r"\b(do not|don't|dont|without|never)\s+(click|touch|press)(ing)?\b|\bread[- ]only\b", re.I)
TOUCHING = ("click", "double_click", "right_click", "type_text", "type_keys", "drag", "draw_across", "hold_key",
            "press_key", VISUAL_ACTION, VISUAL_DRAG)  # scrolling and waiting are free: they change nothing
# Controls with no text field that do something when pressed: if clicked when told "write here", they submit the form.
BUTTON_ROLES = ("button", "hyperlink", "menuitem", "tabitem", "checkbox", "radiobutton", "splitbutton", "a")
BUTTON_WORDS = {"next", "ileri", "sonraki", "devam", "continue", "ok", "tamam", "kaydet", "save", "submit", "gönder",
                "send", "cancel", "iptal", "back", "geri", "yes", "no", "evet", "hayır", "apply", "uygula", "done",
                "bitti", "finish", "close", "kapat", "sign in", "log in", "giriş yap", "oturum aç", "install",
                "yükle", "kur"}  # no "ara/search": text inside a search box looks the same


PLAN_MAX_PARTS = 8  # a goal is split into at most this many steps
PLAN_PART_STEPS = 8  # max actions for a single plan step
# Conjunctions showing that the goal has several tasks. "ve" (and) alone is not enough ("Ara ve Bul" düğmesine tıkla).
_MULTI_RX = re.compile(r"\b(sonra|ardından|ardindan|daha sonra|bunun ardından|son olarak|then|after that|afterwards|"
                       r"and then|finally)\b|;|\b\w{3,}(ıp|ip|up|üp)\b[^.]{3,}(\bve\b|,)", re.I)


def multi_part_goal(goal: str) -> bool:
    """Does the goal ask for several separate tasks? (planning happens only for these: a short goal does not
    waste an extra model call)"""
    words = goal.split()
    if len(words) < 7:
        return False
    return bool(_MULTI_RX.search(goal)) or (len(words) >= 12 and len(re.findall(r"\b(ve|and)\b|,", goal, re.I)) >= 2)


def no_touch_goal(*goals: str) -> bool:
    """Does the goal ask that nothing on the screen be pressed? (a read/observe goal)"""
    return any(NO_TOUCH_RX.search(g or "") for g in goals)


def not_a_field(item: Item) -> bool:
    """Is it an item nothing can be typed into, such as a button? When the page's fields are not listed
    (Google's code box in Chrome), the 'Next' text was picked as the place to type, clicked, and an empty form
    was sent."""
    if item.editable:
        return False
    return item.role.lower() in BUTTON_ROLES or item.label.strip().casefold() in BUTTON_WORDS


def login_item(item: Item, *goals: str) -> bool:
    """Is the item a login/sign-up control, and does the goal not want to log in? (see loop.LOGIN_RX)"""
    return login_text(f"{item.label} {item.value}", *goals)


def goal_keys(goal: str) -> list[str]:
    """Shortcuts written in the goal (e.g. 'ctrl+shift+n') also go into the options."""
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
        self.last: Screen | None = None  # last screen read (for the new texts that appeared, in the game turn summary)
        # Opens a game session: what changed since the start of the turn goes to Jev, and each action's evidence
        # (what appeared, what disappeared) is kept in self.evidence for reflection.
        self.track_changes = False
        # A game move: stop after the first action that changes the screen. In Solitaire the first click had moved 4♣,
        # but Jev did not recognize it and kept clicking; verification is done by the model that reads the board
        # (Voyager, Cradle).
        self.stop_after_change = False
        # If the user explicitly asked (send the message, delete the file), irreversible buttons may be pressed.
        self.allow_irreversible = False
        # Function that asks for the next concrete step when stuck (state dict -> {"next": "...", "give_up": bool}).
        self.consult: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        # Function that splits a multi-part goal into short sequential steps (goal -> step list). See _run_plan.
        self.plan: Callable[[str], list[str]] | None = None
        self.evidence: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers
    def _english(self, goal: str) -> str:
        if self.translate is None:
            return goal
        try:
            out = (self.translate(goal) or "").strip()
            return out or goal
        except Exception:  # noqa: BLE001 - if translation fails, fall back to the original goal
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

    def _steady_item(self, state: dict[str, Any], scr: Screen, ans: Any, images: dict[str, Any] | None,
                     dead: set[str]) -> Item | None:
        """Make item selection more robust (like jev-mcp's thresholds; the decision lives in code):
        - if an item that changed nothing earlier on this screen was picked, the next candidate;
        - if the best candidate is undecided (< ITEM_SURE), take a second look at the best few candidates
          and average the two looks."""
        probs = {k: v for k, v in (ans.probabilities or {}).items()
                 if not any(i.key == k and i.describe(False) in dead for i in scr.items)}
        if not probs:
            return None
        top = sorted(probs, key=probs.get, reverse=True)[:SECOND_LOOK_TOP]
        if probs[top[0]] < ITEM_SURE and len(top) > 1 and probs[top[1]] >= SECOND_LOOK_MIN:
            pool = {i.key: i.describe() for i in scr.items if i.key in top}
            try:
                d = self.decider.decide(state, {"item": Choice("Which item in `items` should the next action be done "
                                                               "on, to make progress toward `goal`?", pool)},
                                        images=images)
                second = d["item"].probabilities or {d["item"].value: 1.0}
                total = sum(probs[k] for k in top) or 1.0
                mixed = {k: (probs[k] / total + second.get(k, 0.0)) / 2 for k in top}
                top = sorted(mixed, key=mixed.get, reverse=True)
            except DeciderError:
                pass
        return next((i for i in scr.items if i.key == top[0]), None)

    def _keyboard(self, goal: str, inputs: dict[str, str]) -> str:
        """Text to type into the window with the keyboard: the text from the tool, a single input, or else
        the fast model generates it from the goal."""
        if getattr(self, "_keyboard_text", ""):
            return self._keyboard_text
        if len(inputs) == 1:
            return next(iter(inputs.values()))
        if self.text_writer is None:
            raise DeciderError("klavyeyle yazılacak metin yok (text ver)")
        return self.text_writer(goal, "the whole window, typed with the keyboard (no field is selected)")

    def _questions(self, scr: Screen, keys: list[str], see: bool,
                   compare: tuple[str, str] | None = None) -> dict[str, Any]:
        items = {i.key: i.describe() for i in scr.items}
        kinds = dict(ACTION_CRITERIA)
        if not any(i.editable for i in scr.items) and not any(i.source == "ocr" for i in scr.items):
            kinds.pop("type_text")  # no input field to type into
        if len(scr.items) < 2:
            kinds.pop("drag")
        if not callable(getattr(self.obs, "drag_at", None)):
            kinds.pop("draw_across")
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
        # Choice needs at least two options; with only one, it is not asked and the code uses it (_pick).
        if len(items) > 1:
            q["item"] = Choice("Which item in `items` should the next action be done on, to make progress toward "
                               "`goal`?", items)
            q["drop"] = Choice("If an item is dragged next, onto which item in `items` should it be dropped?", items)
        fields = {i.key: i.describe() for i in scr.items if i.editable}
        if len(fields) > 1:
            q["field"] = Choice("If text is typed next, into which input field should it be typed for `goal`?", fields)
        if see:
            q["target_visible"] = Noul("Is the thing `goal` needs to click next visible in `screen`?")
            q["target"] = Box(grab_box("clicked"))
            if VISUAL_DRAG in kinds:
                # Cards, pieces and units are not text: they are not in the list, so they must be found on
                # screen to be dragged.
                q["drag_visible"] = Noul("Is the thing `goal` needs to drag next visible in `screen`?")
                q["drag_source"] = Box(grab_box("grabbed and dragged"))
                q["drop_place"] = Box("the place in `screen` where the dragged thing should be dropped for `goal`")
            if "draw_across" in kinds:
                q["draw_area"] = Box("the empty area in `screen`, such as a canvas, picture or map, where `goal` "
                                     "wants to draw or select")
        if see and compare:
            # "Fully achieved?" stayed abstract (0.1 even though a card was drawn in Solitaire). The one observable
            # verdict: is the requested change absent from the previous frame and present in the current one? If there
            # is a changed region, large crops of it are compared (EFR): in the shrunk full screen, the card's number
            # was left at a few pixels.
            before, after = compare
            q["changed_as_asked"] = Noul(f"Does `{after}` show the change `goal` asks for, which `{before}` "
                                         "does not show?", {
                "true": f"Something `goal` asks to happen, such as a card drawn, a piece moved or a screen opened, is "
                        f"visible in `{after}` but not in `{before}`.",
                "false": f"Everything `goal` is about looks the same in `{after}` as in `{before}`, or the "
                         "difference is something else."})
        return q

    # ------------------------------------------------------------------ loop
    def run(self, goal: str, max_steps: int = 25, inputs: dict[str, str] | None = None,
            notes: str | Callable[[Screen], str] = "", text: str = "", keys: list[str] | None = None,
            press_first: list[str] | None = None) -> ReflexResult:
        """notes: fixed text, or a function that picks notes for each step based on the screen
        (in games: the lessons that match what is on screen).
        text: text typed into the window with the keyboard (type_keys). keys: keys/combinations added to the options."""
        self._keyboard_text, self._extra_keys, self._first = text, list(keys or []), None
        self._press_first = list(press_first or [])  # before typing: e.g. escape (so previous input does not stay)
        parts = self._plan_parts(goal) if not (text or press_first) else []
        if len(parts) >= 2:
            res = self._run_plan(goal, parts, max_steps, inputs or {}, notes)
        else:
            res = self._run(goal, max_steps, inputs or {}, notes)
            res = self._consult_loop(goal, max_steps, inputs or {}, notes, res)
        if self._first is not None and self.last is not None:
            # The part of the screen that changed since the start goes into the report: the main model used to spend
            # a separate reflex and screenshot turn just to read the result (e.g. the number on a calculator).
            res.changes = screen_changes(self._first.items, self.last.items)
        # Result line: so the trace shows whether "clicked"/"done" came at the right moment.
        self._trace({"ts": time.time(), "goal": goal, "result": res.status, "steps": res.steps, "reason": res.reason,
                     "window": res.title})
        return res

    def _plan_parts(self, goal: str) -> list[str]:
        """Hedef birkaç iş içeriyorsa sıralı kısa adımları; değilse (ya da planlanamadıysa) boş liste. Oyun
        oturumunda (hamle + yansıtma kendi düzeniyle çalışır) ve okuma hedefinde planlanmaz."""
        if (self.plan is None or self.track_changes or self.stop_after_change or not multi_part_goal(goal)
                or no_touch_goal(goal)):
            return []
        try:
            parts = [" ".join(str(p).split()) for p in (self.plan(goal) or [])]
        except Exception:  # noqa: BLE001 - plan çıkarılamadıysa eski yol: hedef tek parça denenir
            return []
        parts = [p for p in parts if p][:PLAN_MAX_PARTS]
        return parts if len(parts) >= 2 else []

    def _run_plan(self, goal: str, parts: list[str], max_steps: int, inputs: dict[str, str],
                  notes: Any) -> ReflexResult:
        """Çok parçalı hedefi adım adım yürüt. İzlerde uzun hedeflerin "bitti?" yargısı iş bittiği halde %1-6'da
        kalıyor, refleks adım sınırına kadar tıklamaya devam ediyordu ("1'den 10'a yaz ve kaydet": 20 adım, 26 Ekim);
        tek işlik hedeflerde ise yargı doğru çalışıyor. Her adım kendi kısa hedefiyle yürür ve ayrı doğrulanır; biri
        yapılamazsa orada durulur ve hangisinde kalındığı söylenir."""
        log: list[str] = []
        used = 0
        unsure: list[int] = []
        res = ReflexResult("needs_help", 0, "plan boş", log)
        self.emit("plan: " + " | ".join(f"{n}. {p}" for n, p in enumerate(parts, 1)))
        for n, part in enumerate(parts, 1):
            if used >= max_steps:
                return ReflexResult("max_steps", used, f"adım limiti doldu: planın {n - 1}/{len(parts)} adımı yapıldı, "
                                    f"sıradaki: {part}", log, res.title, res.location)
            log.append(f"(plan {n}/{len(parts)}) {part}")
            budget = min(PLAN_PART_STEPS, max_steps - used)
            res = self._run(part, budget, inputs, notes)
            res = self._consult_loop(part, budget, inputs, notes, res)
            log += res.log
            used += res.steps
            if res.status in ("done", "clicked", "likely_done", "acted"):
                continue
            if res.status == "max_steps" and any("(ekran değişti)" in line for line in res.log):
                # Adımın eylemleri ekranı değiştirdi ama "bitti" denmedi: emin olunamadan sıradakine geçilir, sonuçta
                # bu da söylenir.
                unsure.append(n)
                continue
            where = f"planın {n}. adımı ({n}/{len(parts)}) yapılamadı: {part} - {res.reason}"
            status = res.status if res.status in ("error", "needs_confirmation", "needs_login") else "needs_help"
            return ReflexResult(status, used, where, log, res.title, res.location)
        if unsure:
            return ReflexResult("steps_done", used, f"planın {len(parts)} adımı da denendi; şu adımların bittiğinden emin "
                                f"olunamadı: {', '.join(map(str, unsure))}", log, res.title, res.location)
        return ReflexResult("steps_done", used, f"planın {len(parts)} adımı da yapıldı", log, res.title, res.location)

    def _consult_loop(self, goal: str, max_steps: int, inputs: dict[str, str], notes: Any,
                      res: ReflexResult) -> ReflexResult:
        """Takılınca içeriden danış (jev-mcp'nin consult'u): ana modele tam bir tur harcatıp yeni refleks başlatmak
        yerine kısa bir istemle sıradaki somut adımı sor, onu yap, asıl hedefe devam et. Genel sınavda sürenin çoğu
        needs_help -> ana model turu -> yeni refleks döngüsündeydi."""
        if self.consult is None:
            return res
        log, steps = list(res.log), res.steps
        for n in range(1, MAX_CONSULTS + 1):
            if res.status != "needs_help" or steps >= max_steps:
                break
            situation = {"goal": goal, "window": res.title, "stuck_because": res.reason, "recent_steps": log[-8:],
                         "on_screen": [i.describe(False) for i in (self.last.items if self.last else [])][:40]}
            try:
                advice = self.consult(situation) or {}
            except Exception:  # noqa: BLE001 - danışma olmazsa ana modele dönülür
                break
            answer = " ".join(str(advice.get("answer") or "").split())
            if answer:
                # Hedef bir şeyi okumaktı ve ekranda görünüyor (Gece ışığı: "Off"): danışman sıradaki adım yerine
                # cevabı "adım" diye dönünce refleks onu hedef gibi denemişti.
                log.append(f"(danışma {n}) ekranda: {answer}")
                return ReflexResult("done", steps, f"ekranda görülen: {answer}", log, res.title, res.location)
            hint = " ".join(str(advice.get("next") or "").split())
            if not hint or advice.get("give_up"):
                break
            self.emit(f"danışma {n}: {hint}")
            log.append(f"(danışma {n}) {hint}")
            sub = self._run(hint, min(CONSULT_STEPS, max_steps - steps), inputs, notes)
            log += sub.log
            steps += sub.steps
            if sub.status in ("error", "needs_confirmation", "needs_login"):
                res = sub
                break
            res = self._run(goal, max(1, max_steps - steps), inputs, notes)
            log += res.log
            steps += res.steps
        res.log, res.steps = log, steps
        return res

    def _run(self, goal: str, max_steps: int, inputs: dict[str, str],
             notes: str | Callable[[Screen], str]) -> ReflexResult:
        en = self._english(goal)
        if en != goal:
            self.emit(f"hedef: {en}")
        keys = list(dict.fromkeys(list(KEYS) + goal_keys(goal) + [k.lower().replace(" ", "")
                                                                   for k in getattr(self, "_extra_keys", [])]))
        log: list[str] = []
        history: list[str] = []  # Jev'e giden okunur geçmiş
        tried: dict[str, int] = {}  # eylem -> ekran değiştirmeden kaç kez denendi
        visits: dict[tuple[str, ...], int] = {}  # (ekran imzası, eylem) -> kaç kez yapıldı
        dead: dict[str, set[str]] = {}  # ekran imzası -> o ekranda bir şey değiştirmeyen öğeler
        last_item = ""  # son eylemin öğesi (işe yaramazsa dead'e girer)
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
                    if last_item:
                        # Bu ekranda işe yaramadı: aynı ekranda bir daha seçilmez, sıradaki aday denenir (jev-mcp gibi).
                        dead.setdefault(last_sig, set()).add(last_item)
                    tried[did] = tried.get(did, 0) + 1
                    if tried[did] >= 2:
                        return ReflexResult("needs_help", step - 1, f"aynı eylem iki kez ekranı değiştirmedi: {did}",
                                            log, scr.title, scr.location)
            last_sig = sig
            last_item = ""
            prev_items, prev_image = scr.items, scr.image
            if getattr(self, "_first", None) is None:
                self._first = scr
            if start_items is None:
                start_items, start_image = scr.items, scr.image
            if step == 1 and getattr(self, "_keyboard_text", "") and not wrong_field(scr.focused, self._keyboard_text):
                # Ana model "bunu klavyeyle yaz" dediyse karar Jev'in değil: Hesap Makinesi'nde text='2^20=' verildiği
                # halde Jev "İki" tuşuna tıklamayı seçmişti. Önce yazılır, Jev kalanından devam eder.
                try:
                    # Önce temizle: Hesap Makinesi'nde önceki işlemden kalan "2"nin arkasına yazılmıştı (212345678).
                    for k in getattr(self, "_press_first", []):
                        self.obs.act("press_key", None, keys=k)
                    self.obs.act("type_keys", None, text=self._keyboard_text)
                except Exception as e:  # noqa: BLE001
                    log.append(f"{step}. klavyeyle yazma -> HATA {e}")
                    history.append(f"type_keys failed: {str(e)[:120]}")
                else:
                    first = getattr(self, "_press_first", [])
                    history.append((f"pressed {', '.join(first)}, then " if first else "")
                                   + f"typed '{self._keyboard_text[:40]}' on the keyboard")
                    log.append(f"{step}. {history[-1]}")
                self._keyboard_text = ""
                time.sleep(self.settle)
                continue
            see = self._can_see(scr)
            state: dict[str, Any] = {"goal": en, "window": scr.title, "items": {i.key: i.describe() for i in scr.items},
                                     "recent_actions": history[-6:] or ["none yet"]}
            if scr.focused:
                state["keyboard_focus"] = scr.focused  # kodun bildiği gerçek: yazılan metin nereye gider
            if dead.get(sig):
                # Kodun bildiği gerçek (typesafe-computer-use: "actions already tried on the current screen").
                state["tried_here_without_effect"] = sorted(dead[sig])
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
            if item is not None and "item" in a:
                item = self._steady_item(state, scr, a["item"], images, dead.get(sig, set())) or item
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
            if choice in TOUCHING and no_touch_goal(goal, en):
                # Okuma hedefi ("durumu oku, hiçbir şeye basma"): Jev yine de 'Off' yazısına tıklamayı seçiyordu
                # (26 Ekim, Gece ışığı). Dokunulmaz; danışma ekranda görüneni cevap olarak okur.
                return ReflexResult("needs_help", step - 1, "hedef ekrana dokunmayı yasaklıyor; hiçbir şeye "
                                    "basılmadı, ekrandakiler okunabilir", log, scr.title, scr.location)
            if choice == VISUAL_DRAG:
                res = self._visual_drag(step, scr, en, a, log, history)
                if isinstance(res, ReflexResult):
                    return res
                time.sleep(self.settle)
                continue
            if choice == "draw_across":
                res = self._draw_across(step, scr, item, see, a, log, history)
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
            # Öğe apaçık, tür kararsız: WhatsApp'ta arama kutusu 0.95 ile seçildi ama tıkla/yaz/kaydır oyu bölündü
            # (tıkla 0.35) ve pes edildi. En olası tür tıklamaysa tıklanır (geri alınamaz düğme kapısı yine geçerli).
            clear_click = (choice == "click" and item is not None and "item" in a
                           and a["item"].confidence >= ITEM_CLEAR and act.confidence >= KIND_FLOOR)
            if act.confidence < KIND_MIN and not clear_click:
                return ReflexResult("needs_help", step - 1, f"ne yapılacağından emin değil (güven {act.confidence:.2f}"
                                    f", en olası: {choice})", log, scr.title, scr.location)
            if choice == "type_text":
                # Yazılacak yer ayrı sorulur: "item" sorusunda Jev Gezgin'in sol ağacındaki 'Desktop'u seçip oraya
                # "yazdırıyordu" (tıklandı, Masaüstü açıldı).
                item = _pick(a, "field", [i for i in scr.items if i.editable]) or item
                if item is not None and not_a_field(item):
                    # Listede yazı alanı yok ve seçilen şey bir düğme: tıklanıp yazılırsa form boş gönderilir.
                    return ReflexResult("needs_help", step - 1, f"yazılacak alan listede yok ('{item.label[:40]}' bir "
                                        "düğme, alan değil); alanı kendin tıklayıp metni text= ile klavyeden yazdır",
                                        log, scr.title, scr.location)
            needs_item = choice in ("click", "double_click", "right_click", "type_text", "drag")
            if needs_item and item is None:
                return ReflexResult("needs_help", step - 1, "ekranda seçilecek öğe yok", log, scr.title, scr.location)
            if choice in ("click", "double_click", "drag") and item is not None and not self.allow_irreversible \
                    and RISKY_RX.search(item.label):
                # Geri alınamaz olabilecek adım: kullanıcının açık isteği olmadan yapılmaz (jev-mcp'nin güvenlik
                # kapısı gibi; karar kodda). Solitaire'in gizlilik onayı başka araçla kabul edilmişti.
                return ReflexResult("needs_confirmation", step - 1, f"'{item.label[:60]}' geri alınamaz bir iş yapabilir "
                                    "(gönderme, ödeme, silme, onay/kabul)", log, scr.title, scr.location)
            if choice in ("click", "double_click", "type_text") and item is not None and login_item(item, goal, en):
                return ReflexResult("needs_login", step - 1, f"giriş ekranı: '{item.label[:60]}' kullanıcının "
                                    "girişini istiyor", log, scr.title, scr.location)
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
                if choice in ("press_key", "hold_key"):
                    key = str(a["key"].value)
                if choice == "type_keys":
                    text = self._keyboard(en, inputs)
                    if wrong_field(scr.focused, text):
                        # Multi-line text would have gone into a one-line field (file name in a Save dialog left open).
                        history.append(f"did not type: keyboard focus is in {scr.focused}, a one-line field")
                        log.append(f"{step}. yazılmadı: odak tek satırlık alanda ({scr.focused})")
                        continue
                self.obs.act(choice, item if needs_item else None, text=text, keys=key, drop=drop)
            except Exception as e:  # noqa: BLE001
                log.append(f"{step}. {choice} -> HATA {e}")
                history.append(f"{choice} failed: {str(e)[:120]}")
                time.sleep(self.settle)
                continue
            said = {"click": "clicked", "double_click": "double-clicked", "right_click": "right-clicked",
                    "type_text": f"typed '{text[:40]}' into", "drag": "dragged", "press_key": f"pressed {key}",
                    "type_keys": f"typed '{text[:40]}' on the keyboard",
                    "hold_key": f"held {key} for {getattr(self.obs, 'hold_seconds', 1.0):g}s",
                    "scroll_down": "scrolled down", "scroll_up": "scrolled up", "wait": f"waited {WAIT_SECONDS:g}s"}
            what = item.describe(False) if needs_item and item else ""
            if choice == "drag" and drop is not None:
                what += f" onto {drop.describe(False)}"
            history.append(f"{said.get(choice, choice)} {what}".strip())
            last_item = item.describe(False) if needs_item and item else ""
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
        """Target not in the list: Jev's visual answer (is it there? + Box); if unsure, look again up close."""
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

    def _draw_across(self, step: int, scr: Screen, item: Item | None, see: bool, a: dict[str, Any], log: list[str],
                     history: list[str]) -> ReflexResult | None:
        """Drag across an area from edge to edge (to draw, to select an area). In Paint, Jev's drag boxes pointed at
        nearly the same spot in the middle of the canvas: a 2-pixel drag drew no shape. The area is a large item's box
        if there is one, otherwise the empty area Jev saw; from the top-left quarter to the bottom-right quarter."""
        area, what = None, ""
        if item is not None and item.rect and (item.rect[2] - item.rect[0]) * (item.rect[3] - item.rect[1]) >= DRAW_MIN_AREA:
            area, what = item.rect, item.describe(False)
        elif see and "draw_area" in a:
            b = a["draw_area"].value
            sx, sy = padded_fraction(scr.image.size)
            area = (b["left"] * sx, b["top"] * sy, min(1.0, b["right"] * sx), min(1.0, b["bottom"] * sy))
            what = "the area seen on screen"
        if area is None:
            return ReflexResult("needs_help", step - 1, "çizilecek/seçilecek alan bulunamadı", log, scr.title,
                                scr.location)
        l, t, r, b_ = area
        fx, fy, tx, ty = l + 0.25 * (r - l), t + 0.25 * (b_ - t), l + 0.75 * (r - l), t + 0.75 * (b_ - t)
        try:
            self.obs.drag_at(fx, fy, tx, ty)
        except Exception as e:  # noqa: BLE001
            log.append(f"{step}. boydan boya sürükleme -> HATA {e}")
            history.append(f"draw across failed: {str(e)[:120]}")
            return None
        history.append(f"drew across {what} from {place(fx, fy)} to {place(tx, ty)}")
        log.append(f"{step}. {history[-1]}")
        return None

    def _visual_drag(self, step: int, scr: Screen, goal: str, a: dict[str, Any], log: list[str],
                     history: list[str]) -> ReflexResult | None:
        """Pick up something not in the list (card, piece, unit) from where it is seen and drop it where it is seen."""
        seen = a["drag_visible"].value if "drag_visible" in a else 0.0
        if seen < VISIBLE_MIN or "drop_place" not in a:
            return ReflexResult("needs_help", step - 1, f"sürüklenecek şey ekranda bulunamadı (var mı? {seen:.2f})",
                                log, scr.title, scr.location)
        sx, sy = padded_fraction(scr.image.size)

        def center(b: dict[str, float]) -> tuple[float, float]:
            return min(1.0, (b["left"] + b["right"]) / 2 * sx), min(1.0, (b["top"] + b["bottom"]) / 2 * sy)

        (fx, fy), (tx, ty) = center(a["drag_source"].value), center(a["drop_place"].value)
        b = a["drag_source"].value
        box = (min(1.0, b["left"] * sx), min(1.0, b["top"] * sy), min(1.0, b["right"] * sx), min(1.0, b["bottom"] * sy))
        close = self._zoom(goal, scr.image, box, "grabbed and dragged")  # take a close look at the spot to grab
        if close is not None and close[0] >= VISIBLE_MIN:
            fx, fy = close[1], close[2]
        try:
            self.obs.drag_at(fx, fy, tx, ty)
        except Exception as e:  # noqa: BLE001
            log.append(f"{step}. ekranda sürükleme -> HATA {e}")
            history.append(f"drag on screen failed: {str(e)[:120]}")
            return None
        log.append(f"{step}. ekranda görülen şey sürüklendi ({fx:.2f}, {fy:.2f}) -> ({tx:.2f}, {ty:.2f})")
        history.append(f"dragged something visible on screen from {place(fx, fy)} to {place(tx, ty)}")
        return None

    def _zoom(self, goal: str, img: Any, box: tuple[float, float, float, float],
              verb: str = "clicked") -> tuple[float, float, float] | None:
        l, t, r, b = box
        w, h = img.size
        hw, hh = max((r - l) * 1.5, 0.15), max((b - t) * 1.5, 0.2)
        cx, cy = min(max((l + r) / 2, hw), 1 - hw), min(max((t + b) / 2, hh), 1 - hh)
        x0, y0, x1, y1 = max(0.0, cx - hw), max(0.0, cy - hh), min(1.0, cx + hw), min(1.0, cy + hh)
        crop = img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
        try:
            d = self.decider.decide({"goal": goal, "screen": ZOOM_REF, "note": "`screen` is a zoomed-in part of the window"},
                                    {"target_visible": Noul(f"Is the thing that should be {verb} next for `goal` "
                                                            "visible in `screen`?"),
                                     "target": Box(grab_box(verb))},
                                    images={ZOOM_REF: prepare_image(crop)})
        except DeciderError:
            return None
        zb = d["target"].value
        sx, sy = padded_fraction(crop.size)
        fx = x0 + (zb["left"] + zb["right"]) / 2 * sx * (x1 - x0)
        fy = y0 + (zb["top"] + zb["bottom"]) / 2 * sy * (y1 - y0)
        return d["target_visible"].value, min(1.0, fx), min(1.0, fy)


def make_consultant(router) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """One short call for a stuck reflex: no chat history, only the goal and the current screen. Much faster than a
    full turn of the main model with all of its tool definitions."""
    import json as _json

    def consult(situation: dict[str, Any]) -> dict[str, Any]:
        res = router.chat("brain", [
            {"role": "system", "content": "A fast on-screen agent got stuck while working toward a goal. From the "
             "situation, give the single next concrete step on this screen, naming exactly what to click, type or "
             "press as it appears on screen (one short sentence). If the goal cannot be reached from here or needs "
             "the user (login, payment, a decision), set give_up. Never suggest sending, paying, deleting or accepting "
             "terms unless the goal explicitly asks for it. If the goal only asks to read or check something and the "
             "screen already shows it, put what it shows in answer and leave next empty. Answer with JSON only: "
             '{"next": "...", "answer": "", "give_up": false}'},
            {"role": "user", "content": _json.dumps(situation, ensure_ascii=False)}], temperature=0, max_tokens=200)
        m = re.search(r"\{.*\}", res.text or "", re.S)
        return _json.loads(m.group(0)) if m else {}

    return consult


def make_planner(router) -> Callable[[str], list[str]]:
    """Split a multi-part goal into short ordered steps (one short call, no screen; steps come from its words)."""
    import json as _json

    def plan(goal: str) -> list[str]:
        res = router.chat("brain", [
            {"role": "system", "content": "Split the user's instruction for an on-screen computer-use agent into the "
             "ordered steps it contains. Each step is one small thing to do in the app (open a menu item, type a given "
             "text, press a shortcut, fill one dialog and confirm it), written as a short imperative sentence in the "
             "instruction's own language. Keep names of buttons, menus, files, paths and any text to type exactly as "
             "written, and repeat in every step the details it needs (the file path, the text). Do not add steps the "
             "instruction does not ask for, do not add verification steps, and keep any restriction (do not send, do "
             f"not overwrite) in the step it applies to. At most {PLAN_MAX_PARTS} steps. If the instruction is a "
             'single action, return it as one step. Answer with JSON only: {"steps": ["...", "..."]}'},
            {"role": "user", "content": goal}], temperature=0, max_tokens=500)
        m = re.search(r"\{.*\}", res.text or "", re.S)
        steps = _json.loads(m.group(0)).get("steps") if m else None
        return [str(s) for s in steps] if isinstance(steps, list) else []

    return plan


def make_translator(router) -> Callable[[str], str]:
    """Translate the goal into English (Jev is best in English). Names on screen and quoted text stay unchanged."""

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
