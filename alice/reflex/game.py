"""Oyun oturumu: strateji notları, sadece değişen kısımlara bakan gözlemci ve tur özetleri.

Tek bir reflex çağrısı oyunu bilmez: her seferinde ekranı sıfırdan okur, önceki turda ne olduğunu, oyunun kurallarını
ve stratejiyi hatırlamaz. Oturum bunları turlar boyunca taşır:

- notlar (`~/.alice/games/<oyun>/notes.md`): oyunun arayüzü, kurallar, strateji. Oturumlar arası kalır; beyin modeli
  ya da kullanıcı öğrendikçe ekler. Her turda Jev'e `notes` olarak gider (İngilizce daha iyi);
- gözlemci tur boyunca ve turlar arasında yaşar: önceki kareyle karşılaştırıp yalnızca değişen bölgeleri yeniden okur,
  yeni çıkan öğeleri 'new' diye işaretler;
- her turdan sonra kısa bir özet (`turns.jsonl`); son birkaç özet sonraki turların bağlamına girer.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

RECENT_TURNS = 3  # sonraki turun bağlamına giren özet sayısı
NOTES_MAX = 4000  # karakter: Jev'e giden notların üst sınırı (en yeniler kalır)
SUMMARY_MIN_WORDS = 4

# Notu olmayan bir oyuna başlarken. Kullanıcı ya da beyin modeli notes.md'yi düzenledikçe bunun yerini alır.
STARTER_NOTES = {
    "unciv": [
        "Unciv is a turn-based 4X strategy game like Civilization V.",
        "On the map screen the big button at the top-right says what the turn still needs: 'Pick a tech', "
        "'Pick construction', 'Next unit' or, when nothing is left, 'Next turn'. Clicking it handles that thing or "
        "ends the turn. It is not shown inside the city or tech screens: leave them first ('Exit City', back).",
        "Choosing research or city production opens a list: pick one entry, then confirm with the button at the top "
        "or bottom of that screen.",
        "In the city screen 'Pick a construction' is only a label: click an entry in the list on the left (e.g. "
        "'Warrior') to build it, then 'Exit City'.",
        "Units are icons on the map, not text: select one by clicking its icon, then pick an action from the unit "
        "panel at the bottom-left (e.g. 'Found city', 'Fortify', 'Explore', 'Sleep').",
        "Escape or the back/close button leaves a sub-screen. Never quit the game or open the main menu.",
    ],
}


def game_key(name: str) -> str:
    """'Unciv' / 'unciv oyunu' -> 'unciv' (klasör adı)."""
    s = re.sub(r"[^\w]+", "-", (name or "").strip().lower(), flags=re.U).strip("-")
    return s or "game"


@dataclass
class GameSession:
    name: str
    title: str  # pencere başlığından parça
    root: Path
    turn: int = 0
    started: float = field(default_factory=time.time)
    observer: Any = None  # diff kipindeki gözlemci: turlar arasında yaşar

    @classmethod
    def open(cls, home: Path, name: str, title: str = "") -> "GameSession":
        key = game_key(name)
        root = home / "games" / key
        root.mkdir(parents=True, exist_ok=True)
        notes = root / "notes.md"
        if not notes.exists():
            starter = STARTER_NOTES.get(key.split("-")[0], [])
            notes.write_text("".join(f"- {n}\n" for n in starter), encoding="utf-8")
        s = cls(name, title or name, root)
        s.turn = len(s.turns())
        return s

    # ---------------------------------------------------------------- notlar
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

    # ---------------------------------------------------------------- turlar
    @property
    def turns_path(self) -> Path:
        return self.root / "turns.jsonl"

    def turns(self) -> list[dict[str, Any]]:
        try:
            rows = self.turns_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for r in rows:
            try:
                out.append(json.loads(r))
            except ValueError:
                continue
        return out

    def record(self, goal: str, status: str, steps: int, summary: str) -> dict[str, Any]:
        self.turn += 1
        row = {"ts": time.time(), "turn": self.turn, "goal": goal, "status": status, "steps": steps,
               "summary": summary}
        with self.turns_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def context(self) -> str:
        """Jev'e giden notes: oyunun notları + son turların özetleri."""
        notes = "\n".join(f"- {n}" for n in self.notes())
        if len(notes) > NOTES_MAX:
            notes = notes[-NOTES_MAX:].split("\n", 1)[-1]
        recent = [t for t in self.turns() if t.get("summary")][-RECENT_TURNS:]
        parts = [f"Game: {self.name}."]
        if notes:
            parts.append("Notes about this game:\n" + notes)
        if recent:
            parts.append("Recent turns:\n" + "\n".join(f"- turn {t['turn']}: {t['summary']}" for t in recent))
        return "\n\n".join(parts)


def summarize_turn(chat: Callable[[list[dict[str, str]]], str] | None, goal: str, status: str, log: list[str],
                   new_texts: list[str]) -> str:
    """Tur özeti (1-2 cümle, İngilizce: sonraki turda Jev'e gidiyor). Model yoksa ya da hata verirse kayıttan kurulur."""
    fallback = f"{goal} -> {status}" + (f"; last: {log[-1]}" if log else "")
    if chat is None:
        return fallback[:300]
    facts = (f"Goal of this turn: {goal}\nResult: {status}\nActions:\n" + "\n".join(log[-12:] or ["(none)"])
             + ("\nNew on screen at the end: " + "; ".join(new_texts[:15]) if new_texts else ""))
    try:
        out = chat([
            {"role": "system", "content": "You keep a short log of a computer game session. Summarize this turn in "
             "one or two short English sentences: what was done, what changed, and what seems to be needed next. "
             "Use only the facts given; do not invent game events."},
            {"role": "user", "content": facts}]).strip()
    except Exception:  # noqa: BLE001 - özet olmazsa tur yine kaydedilir
        return fallback[:300]
    out = " ".join(out.split())[:400]
    # Hızlı model bazen yarım cevap veriyor (Unciv'de özet sadece "The" geldi): kayıttan kurulanı kullan.
    return out if len(out.split()) >= SUMMARY_MIN_WORDS else fallback[:300]
