"""Oyun oturumu: oyunu oynarken kendisi öğrenir. Koda oyuna özel bilgi yazılmaz (bkz. docs/OYUN-OGRENME.md).

Tek bir reflex çağrısı oyunu bilmez: her seferinde ekranı sıfırdan okur, önceki turu, oyunun arayüzünü ve stratejiyi
hatırlamaz. Oturum bunları turlar ve oturumlar boyunca taşır:

- dersler (`~/.alice/games/<oyun>/lessons.jsonl`): "ekranda şu görünüyorsa (trigger) şunu yap/yapma (do)". ExpeL / ERL
  gibi her turdan sonra kanıta bakılarak güncellenir: eklenir, doğrulanır (puan +1), çelişince düşer. Her adımda
  Jev'e sadece ekrandakine uyan dersler gider;
- keşif: hiç dersi olmayan oyunda ilk ekran ve modelin oyun hakkında bildikleriyle başlangıç dersleri yazılır;
  bunlar doğrulanmamış (prior) sayılır, oyunda karşılığı görülünce güçlenir;
- kanıt kodda çıkarılır: her eylemden sonra ekranda ne çıktı, ne kayboldu, hiçbir şey değişti mi. Model bunun dışında
  bir oyun olayı uyduramaz; aynı eylemin iki kez hiçbir şey yapmaması gibi kesin dersleri kod kendisi yazar;
- gözlemci turlar arasında yaşar (yalnızca değişen bölgeler yeniden okunur); her turdan sonra kısa özet
  (`turns.jsonl`), son üç özet sonraki turların bağlamına girer;
- `notes.md`: kullanıcının ya da beyin modelin elle yazdığı notlar, her zaman gider.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from alice.reflex.perceive import words

RECENT_TURNS = 3  # sonraki turun bağlamına giren özet sayısı
NOTES_MAX = 4000  # karakter: Jev'e giden notların üst sınırı
LESSONS_SHOWN = 10  # bir adımda Jev'e giden en fazla ders
LESSONS_MAX = 80  # dosyada tutulan en fazla ders (fazlası: puanı en düşük, en eski olan düşer)
ADDS_PER_TURN = 3  # bir turdan en fazla bu kadar yeni ders
TRIGGER_SHARE = 0.6  # tetiğin kelimelerinin bu kadarı ekranda görünüyorsa ders bu ekrana uyar
SUMMARY_MIN_WORDS = 4
CLICK_RX = re.compile(r"^(?:clicked|double-clicked|right-clicked) \w+ '([^']+)'")
# Her oyunda geçerli, koddan gelen tek kural: keşif bir keresinde "istenirse Microsoft hesabıyla giriş yap" yazdı ve
# Solitaire'in açılışındaki gizlilik onayı kabul edildi. Oyun oturumu kullanıcı adına hiçbir şeyi onaylamaz.
SAFETY = ("Never sign in, accept terms or consent screens, buy anything or click ads; if one of these blocks the game, "
          "stop and leave it to the user.")
FORBIDDEN = "Never suggest signing in, accepting terms or consent screens, purchases or ads."
# Model ne derse desin bu yönde bir "yap" dersi kaydedilmez ("avoid/never ..." diyen ders kalır).
UNSAFE_RX = re.compile(r"^(?!.*\b(?:avoid|never|do not|don't)\b).*\b(?:sign ?in|log ?in|accept|agree|purchase|buy|"
                       r"subscribe|watch (?:an )?ad)", re.I)

Chat = Callable[[list[dict[str, str]]], str]


def game_key(name: str) -> str:
    """'Unciv' / 'Slay the Spire!' -> 'unciv' / 'slay-the-spire' (klasör adı)."""
    s = re.sub(r"[^\w]+", "-", (name or "").strip().lower(), flags=re.U).strip("-")
    return s or "game"


def _json_block(text: str) -> dict[str, Any]:
    """Model cevabındaki ilk JSON nesnesi (kod bloğu içinde de olabilir); yoksa {}."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        out = json.loads(m.group(0))
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


@dataclass
class GameSession:
    name: str
    title: str  # pencere başlığından parça
    root: Path
    turn: int = 0
    observer: Any = None  # fark kipindeki gözlemci: turlar arasında yaşar

    @classmethod
    def open(cls, home: Path, name: str, title: str = "") -> "GameSession":
        root = home / "games" / game_key(name)
        root.mkdir(parents=True, exist_ok=True)
        s = cls(name, title or name, root)
        s.turn = len(s.turns())
        return s

    # ---------------------------------------------------------------- elle yazılan notlar
    @property
    def notes_path(self) -> Path:
        return self.root / "notes.md"

    def notes(self) -> list[str]:
        try:
            text = self.notes_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [ln[2:].strip() if ln.startswith("- ") else ln.strip() for ln in text.splitlines() if ln.strip()]

    def add_note(self, note: str) -> bool:
        """Notu ekle (aynısı varsa ekleme). Eklendiyse True."""
        note = " ".join((note or "").split())
        if not note or note.lower() in (n.lower() for n in self.notes()):
            return False
        with self.notes_path.open("a", encoding="utf-8") as f:
            f.write(f"- {note}\n")
        return True

    # ---------------------------------------------------------------- dersler
    @property
    def lessons_path(self) -> Path:
        return self.root / "lessons.jsonl"

    def lessons(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.lessons_path)

    def _save_lessons(self, rows: list[dict[str, Any]]) -> None:
        if len(rows) > LESSONS_MAX:
            rows = sorted(rows, key=lambda r: (r.get("score", 0), r.get("ts", 0)), reverse=True)[:LESSONS_MAX]
        self.lessons_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def apply(self, ops: dict[str, Any], source: str = "seen", seen_texts: list[str] | None = None) -> dict[str, int]:
        """Model ya da kodun önerdiği değişiklikleri uygula: add / confirm / contradict / edit. Uygulanan sayıları döner.

        Puan: gözlenen ders 1'den, doğrulanmamış (prior) 0'dan başlar; confirm +1 (prior'ı gözlenene çevirir),
        contradict -2; puanı sıfırın altına düşen ders silinir.
        seen_texts: bu turda ekranda görülen yazılar. Tetiği bunlarda geçmeyen yeni ders alınmaz: model "clicked
        something visible at left-top" gibi kendi cümlesini tetik yazmıştı, o ders hiçbir ekrana uymaz."""
        rows = self.lessons()
        by_id = {r["id"]: r for r in rows}
        done = {"add": 0, "confirm": 0, "contradict": 0, "edit": 0}
        for lid in ops.get("confirm") or []:
            r = by_id.get(str(lid))
            if r:
                r["score"] = r.get("score", 0) + 1
                r["source"] = "seen"
                done["confirm"] += 1
        for lid in ops.get("contradict") or []:
            r = by_id.get(str(lid))
            if r:
                r["score"] = r.get("score", 0) - 2
                done["contradict"] += 1
        for e in ops.get("edit") or []:
            r = by_id.get(str(e.get("id", ""))) if isinstance(e, dict) else None
            if r:
                for k in ("trigger", "do"):
                    if isinstance(e.get(k), str) and e[k].strip():
                        r[k] = " ".join(e[k].split())
                done["edit"] += 1
        rows = [r for r in rows if r.get("score", 0) >= 0]
        seen = {(r.get("trigger", "").lower(), r.get("do", "").lower()) for r in rows}
        next_id = max([int(r["id"][1:]) for r in rows if str(r.get("id", "")).startswith("l")
                       and str(r["id"])[1:].isdigit()] + [0]) + 1
        for a in (ops.get("add") or [])[:ADDS_PER_TURN if source != "prior" else 12]:
            if not isinstance(a, dict) or not str(a.get("do", "")).strip():
                continue
            trigger, do = " ".join(str(a.get("trigger") or "").split()), " ".join(str(a["do"]).split())
            if (trigger.lower(), do.lower()) in seen or UNSAFE_RX.search(do):
                continue
            if trigger and seen_texts is not None and not _on_screen(trigger, seen_texts):
                continue
            kind = a.get("kind") if a.get("kind") in ("ui", "rule", "strategy") else "ui"
            rows.append({"id": f"l{next_id}", "trigger": trigger, "do": do, "kind": kind,
                         "score": 0 if source == "prior" else 1, "source": source, "ts": time.time()})
            seen.add((trigger.lower(), do.lower()))
            next_id += 1
            done["add"] += 1
        self._save_lessons(rows)
        return done

    def relevant(self, visible: list[str] | None, limit: int = LESSONS_SHOWN) -> list[dict[str, Any]]:
        """Bu ekrana uyan dersler: tetiği ekranda görünenler önce, sonra tetiksiz (her zaman geçerli) olanlar; puanı
        yüksek olan önce. ERL: hepsini her seferinde göndermek gürültü; ilgili olanları seçmek gerekiyor.
        visible None: ekran yok (start/status), hepsi puan sırasıyla."""
        hits, general = [], []
        for r in self.lessons():
            if not [w for w in words(r.get("trigger", "")) if len(w) > 1]:
                general.append(r)
            elif visible is None or _on_screen(r["trigger"], visible):
                hits.append(r)
        key = lambda r: (-r.get("score", 0), -r.get("ts", 0))  # noqa: E731
        # Oyunun kuralları Jev'e her adımda önce gider (hangi hamlenin yasal olduğu buna bağlı), sonra bu ekrana uyan
        # dersler, sonra diğer genel dersler.
        rules = [r for r in general if r.get("kind") == "rule"]
        rest = [r for r in general if r.get("kind") != "rule"]
        return (sorted(rules, key=key) + sorted(hits, key=key) + sorted(rest, key=key))[:limit]

    # ---------------------------------------------------------------- turlar
    @property
    def turns_path(self) -> Path:
        return self.root / "turns.jsonl"

    def turns(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.turns_path)

    def record(self, goal: str, status: str, steps: int, summary: str) -> dict[str, Any]:
        self.turn += 1
        row = {"ts": time.time(), "turn": self.turn, "goal": goal, "status": status, "steps": steps,
               "summary": summary}
        with self.turns_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def context(self, visible: list[str] | None = None) -> str:
        """Jev'e giden notes: elle yazılan notlar + bu ekrana uyan dersler + son turların özetleri."""
        parts = [f"Game: {self.name}. {SAFETY}"]
        notes = "\n".join(f"- {n}" for n in self.notes())
        if notes:
            parts.append("Player notes:\n" + notes[-NOTES_MAX:])
        lessons = self.relevant(visible)
        if lessons:
            parts.append("Lessons learned while playing (most relevant first):\n" + "\n".join(
                f"- {'When ' + repr(r['trigger']) + ' is on screen: ' if r.get('trigger') else ''}{r['do']}"
                + (" (unverified)" if r.get("source") == "prior" else "") for r in lessons))
        recent = [t for t in self.turns() if t.get("summary")][-RECENT_TURNS:]
        if recent:
            parts.append("Recent turns:\n" + "\n".join(f"- turn {t['turn']}: {t['summary']}" for t in recent))
        return "\n\n".join(parts)


def _on_screen(trigger: str, visible: list[str]) -> bool:
    """Tetiğin kelimelerinin çoğu ekrandaki yazılarda geçiyor mu?"""
    tw = [w for w in words(trigger) if len(w) > 1]
    vis: set[str] = set()
    for v in visible:
        vis.update(words(v))
    return bool(tw) and sum(w in vis for w in tw) / len(tw) >= TRIGGER_SHARE


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for r in rows:
        try:
            v = json.loads(r)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


# -------------------------------------------------------------------------------------------- keşif
def discover(chat: Chat | None, game: str, visible: list[str], board: str = "") -> dict[str, Any]:
    """Oyunu tanı ve kurallarını çıkar: ekran (görüntü modelinin okuması + yazılar) + ana modelin oyun bilgisi ->
    hangi oyun/mod olduğu (identified) ve başlangıç dersleri (prior), önce kurallar. Kullanıcının önerisi: oyun açılınca
    ana model ekrana bakıp ne oynadığını anlasın, kuralları Jev'e versin ("Microsoft Solitaire Collection" adıyla model
    hangi oyun olduğunu bilemeyip hiç kural yazmamıştı; masa görülünce Klondike olduğu anlaşılır).
    Model yoksa ya da cevap bozuksa boş: oyun yine oynanır, dersler yansıtmayla birikir."""
    if chat is None:
        return {}
    facts = (f"Program: {game}\n" + (f"What the screen shows (read by a vision model):\n{board}\n" if board else "")
             + "Text visible on screen:\n" + "\n".join(f"- {v}" for v in visible[:40]))
    try:
        out = chat([
            {"role": "system", "content":
             "You prepare an agent to play a computer game it has never played. First work out exactly which game "
             "and mode is on screen (a program can contain several games; the screen decides, e.g. a card layout or "
             "a map). Then, from what you reliably know about that game and from the screen, write short, practical "
             "lessons: first the rules of "
             "play (what moves are legal, how pieces or cards may be placed, how to win or score; kind 'rule', one "
             "rule per lesson, empty trigger), then how a turn or round works in this program, what the main screen "
             "elements are for, and what to avoid (quitting, deleting saves). At most 12 lessons. Skip anything you "
             "are not sure about. If you do not know the game, only describe the first screen. "
             f"{FORBIDDEN} A lesson's trigger is exact text visible on screen when the lesson applies, or empty if "
             "it always applies. If the screen shows no game yet (a menu or launcher), say so in identified and write "
             "only menu lessons. Answer with JSON only: {\"identified\": \"which game and mode, or 'menu'\", "
             "\"add\": [{\"trigger\": \"...\", \"do\": \"...\", \"kind\": \"ui|rule|strategy\"}]}"},
            {"role": "user", "content": facts}])
    except Exception:  # noqa: BLE001 - keşif olmazsa oyun yine oynanır
        return {}
    return _json_block(out)


# -------------------------------------------------------------------------------------------- bakış ve plan
Ask = Callable[[list[dict[str, Any]]], str]  # görüntü de alabilen model (vision rolü)
LOOK_MAX_WIDTH = 2000  # piksel: pencere bundan genişse küçültülür (beyin modelin ekran görüntüsü 1366'ya iniyordu)


def image_part(img: Any, max_width: int = LOOK_MAX_WIDTH) -> dict[str, Any]:
    import base64
    import io

    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=88)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue())
                                               .decode()}}


def rule_text(lessons: list[dict[str, Any]], limit: int = 20) -> str:
    """Oyunun bilinen kuralları (kind=rule), puanı yüksek önce; doğrulanmamışlar işaretli."""
    rules = sorted((r for r in lessons if r.get("kind") == "rule"), key=lambda r: -r.get("score", 0))[:limit]
    return "\n".join(f"- {r['id']}: {r['do']}" + (" (unverified)" if r.get("source") == "prior" else "")
                     for r in rules)


def look_board(ask: Ask | None, game: str, image: Any, lessons: list[dict[str, Any]]) -> str:
    """Oyun penceresini görüntü modeline okut: tahtanın metin hali. Kartlar, taşlar, birimler yazı değil; OCR okumuyor,
    Jev kural bilmiyor. Planlayıcı (ve beyin model) tahtayı buradan bilir. Kurallar verilir ki neyin önemli olduğunu
    (renk, sayı, sıra) bilsin; ama oyun bilgisi yazılmaz."""
    if ask is None or image is None:
        return ""
    rules = rule_text(lessons)
    prompt = (f"This is the window of the computer game '{game}'. Describe the current game state so that someone "
              "who cannot see the screen can choose the next move: every card, piece, unit, tile or counter that "
              "matters, with its exact value (for cards: rank and suit; face-down ones as face-down) and where it is "
              "(use rows/columns or areas like top-left, and order within a pile). Then list buttons and messages. "
              "Report only what you see; write 'unclear' where you cannot read something.")
    if rules:
        prompt += f"\nKnown rules of this game, for what to pay attention to:\n{rules}"
    try:
        return ask([{"role": "user", "content": [{"type": "text", "text": prompt}, image_part(image)]}]).strip()
    except Exception as e:  # noqa: BLE001 - bakış olmazsa plan yapılamaz, söylenir
        return f"(tahta okunamadı: {str(e)[:200]})"


def plan_move(chat: Chat | None, game: str, goal: str, board: str, lessons: list[dict[str, Any]],
              recent: list[str]) -> dict[str, Any]:
    """Tahta + kurallar + hedef -> tek somut hamle: ekranda ne yapılacağı (Jev'in uygulayacağı kısa hedef), neden ve ne
    değişmesi beklendiği. Kural akıl yürütme ister: Jev oynanabilir kartı seçemiyordu (7♣'e tıkladı, 4♣ oynanırdı).
    Karar ve gerekçe açık yazılır ki yansıtma beklenenle olanı kıyaslayıp kuralı düzeltebilsin."""
    if chat is None or not board:
        return {}
    others = "\n".join(f"- {r['do']}" for r in lessons if r.get("kind") != "rule")[:3000]
    facts = (f"Game: {game}\nWhat the player wants now: {goal}\n\nBoard:\n{board}\n\nRules known so far:\n"
             f"{rule_text(lessons) or '(none yet)'}\n\nOther lessons:\n{others or '(none)'}"
             + ("\n\nRecent turns:\n" + "\n".join(recent) if recent else ""))
    try:
        out = chat([
            {"role": "system", "content":
             "You choose the next single move in a computer game for an agent that can click, drag, type and press "
             "keys. Use the board description and the rules; check that the move is legal under them. Prefer moves "
             "the rules mark as good; if no move is possible, say what the game offers instead (for example drawing "
             "a card or ending the turn). Describe the move as one short on-screen action naming what to click or "
             "drag by what is visible (for example: Click the 4 of clubs at the top of the third column). "
             "If the move relies on a rule of the game that is not in the known rules, state that rule in "
             "rules_new (one general sentence each, not about this position). "
             f"{FORBIDDEN} Answer with JSON only: {{\"move\": \"...\", \"why\": \"which rule makes it legal/good\", "
             "\"expect\": \"what should change on screen if it works\", \"rules_used\": [rule ids], "
             "\"rules_new\": [\"...\"]}"},
            {"role": "user", "content": facts}])
    except Exception:  # noqa: BLE001
        return {}
    data = _json_block(out)
    return data if str(data.get("move", "")).strip() else {}


# -------------------------------------------------------------------------------------------- yansıtma
def evidence_lines(evidence: list[dict[str, Any]]) -> list[str]:
    out = []
    for n, e in enumerate(evidence, 1):
        line = f"{n}. {e.get('action', '')}"
        if e.get("appeared"):
            line += " | appeared: " + "; ".join(e["appeared"])
        if e.get("disappeared"):
            line += " | disappeared: " + "; ".join(e["disappeared"])
        if e.get("changed_places"):
            # Yazısız değişim (kart açıldı, birim yürüdü): nerede olduğu, model yorumlasın diye.
            line += " | picture changed at: " + ", ".join(e["changed_places"])
        out.append(line)
    return out


def code_lessons(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Kodun kesin bildiği dersler (model gerekmez):
    - aynı yazıya tıklamak iki kez hiçbir şeyi değiştirmediyse o yazı burada tıklanacak bir şey değil (Unciv'de
      'Pick a construction' başlığına defalarca basılmıştı);
    - aynı şeye bir turda üç kez ve daha fazla basıldıysa ekranlar arasında gidip geliniyor (Solitaire'de 'KLONDIKE'
      kartına dört kez basıldı; ekran her seferinde değiştiği için ilk kurala girmedi)."""
    dead: dict[str, int] = {}
    repeats: dict[str, int] = {}
    for e in evidence:
        action = e.get("action", "")
        m = CLICK_RX.match(action)
        if not m:
            continue
        label = m.group(1)
        repeats[label] = repeats.get(label, 0) + 1
        if not e.get("changed") and action.endswith("nothing changed on screen"):
            dead[label] = dead.get(label, 0) + 1
    add = [{"trigger": label, "kind": "ui",
            "do": f"Clicking '{label}' changes nothing; it is probably only a label. Act on something else."}
           for label, c in dead.items() if c >= 2]
    add += [{"trigger": label, "kind": "ui",
             "do": f"Clicking '{label}' over and over goes back and forth between screens. Click it once, then act "
                   "on what the new screen shows."}
            for label, c in repeats.items() if c >= 3 and label not in dead]
    return {"add": add}


def reflect(chat: Chat | None, game: str, goal: str, status: str, reason: str, evidence: list[dict[str, Any]],
            lessons: list[dict[str, Any]], visible: list[str] | None = None,
            intent: dict[str, Any] | None = None, board_after: str = "") -> tuple[str, dict[str, Any]]:
    """Tur sonu: (özet, ders değişiklikleri). Model yalnızca kodun çıkardığı kanıta dayanır. visible: turun sonunda
    ekrandaki yazılar; tetik bunlardan (ya da kanıttaki yazılardan) seçilmeli. intent (play): planlanan hamle, gerekçesi
    ve beklenen değişiklik; board_after: hamleden sonra tahta. Beklenenle olan kıyaslanıp kurallar düzeltilir
    (Cogito, Ergo Ludo: kuralları oynayarak çıkarmak)."""
    lines = evidence_lines(evidence)
    fallback = f"{goal} -> {status}" + (f"; last: {lines[-1]}" if lines else "")
    if chat is None:
        return fallback[:300], {}
    known = "\n".join(f"{r['id']}: {'[' + r['trigger'] + '] ' if r.get('trigger') else ''}{r['do']}"
                      + (" (unverified)" if r.get("source") == "prior" else "") for r in lessons[:40])
    plan = ""
    if intent:
        plan = (f"Planned move: {intent.get('move', '')}\nWhy it was thought legal/good: {intent.get('why', '')}\n"
                f"Rules it relied on: {', '.join(map(str, intent.get('rules_used') or [])) or '-'}\n"
                f"Expected change: {intent.get('expect', '')}\n")
    facts = (f"Game: {game}\nGoal of this turn: {goal}\n{plan}Result: {status} ({reason})\n"
             f"Actions, each with what appeared and disappeared on screen right after it:\n"
             + ("\n".join(lines) or "(no actions)")
             + ("\n\nText on screen at the end of the turn:\n" + "; ".join(visible[:40]) if visible else "")
             + (f"\n\nBoard after the turn:\n{board_after}" if board_after else "")
             + f"\n\nCurrent lessons:\n{known or '(none)'}")
    try:
        out = chat([
            {"role": "system", "content":
             "You review one turn of an agent playing a computer game and maintain its lessons. Use only the "
             "evidence given: never invent game events. Lessons must be general and reusable in later turns, not a "
             "retelling of this turn. Failures teach the most: an action that changed nothing, going back and forth, "
             "a wrong screen. Answer with JSON only: {\"summary\": \"one or two short English sentences: what was "
             "done, what changed, what seems needed next\", \"add\": [{\"trigger\": \"exact on-screen text when it "
             "applies, or empty\", \"do\": \"...\", \"kind\": \"ui|rule|strategy\"}], \"confirm\": [ids of lessons "
             "this turn's evidence supports], \"contradict\": [ids of lessons the evidence shows are wrong], "
             "\"edit\": [{\"id\": \"...\", \"do\": \"...\"}], \"expected_happened\": true|false|null}. At most 3 new "
             "lessons. If the turn did not reach its "
             "goal, add at least one lesson about what to do differently, or confirm an existing lesson that already "
             "says it. A trigger must be copied from the on-screen text listed above (it is how the lesson is found "
             "again later); a lesson whose trigger is not shown on screen is thrown away. Write lessons in English. "
             "If a planned move is given, set expected_happened to true or false by comparing the expected change "
             "with the board after the turn (the board decides, not the number of clicks); when it happened, confirm "
             "the rules it relied on. When it did not happen, decide from the evidence why: if nothing changed, the "
             "move was probably illegal (contradict or edit the rule that allowed it, or add a corrected rule, kind "
             "'rule', empty trigger); if something else changed, the action probably missed its target (add a ui "
             "lesson, keep the rule). "
             f"{FORBIDDEN}"},
            {"role": "user", "content": facts}])
    except Exception:  # noqa: BLE001 - yansıtma olmazsa tur yine kaydedilir
        return fallback[:300], {}
    data = _json_block(out)
    summary = " ".join(str(data.get("summary") or "").split())[:400]
    if len(summary.split()) < SUMMARY_MIN_WORDS:  # yarım cevap ("The"): kayıttan kurulan
        summary = fallback[:300]
    return summary, data
