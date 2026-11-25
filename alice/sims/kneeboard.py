"""In-game kneeboard pages.

DCS shows the images in Saved Games\\DCS\\Kneeboard\\ on the kneeboard as pages (aircraft-specific ones go under
Kneeboard\\<aircraft type>\\). ALICE turns the frequency and waypoints from the mission file, plus notes you add
by voice, into pages. Notes are kept in ~/.alice/kneeboard-notes.json; the pages are redrawn on every change.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from alice.config import alice_home
from alice.i18n import spoken
from alice.sims.dcs import MissionInfo

W, H = 768, 1024
PREFIX = "ALICE_"

FONTS = ["arial.ttf", "segoeui.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/System/Library/Fonts/Supplemental/Arial.ttf"]
MONO = ["consola.ttf", "cour.ttf", "DejaVuSansMono.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]


def _font(size: int, mono: bool = False):
    from PIL import ImageFont

    for name in (MONO if mono else []) + FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def notes_path() -> Path:
    return alice_home() / "kneeboard-notes.json"


def load_notes() -> list[dict]:
    try:
        return json.loads(notes_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def add_note(text: str) -> list[dict]:
    notes = load_notes() + [{"time": time.strftime("%H:%M"), "text": text.strip()}]
    notes_path().parent.mkdir(parents=True, exist_ok=True)
    notes_path().write_text(json.dumps(notes, ensure_ascii=False), encoding="utf-8")
    return notes


def clear_notes() -> None:
    try:
        notes_path().unlink()
    except OSError:
        pass  # no notes to clear


class Page:
    def __init__(self, title: str):
        from PIL import Image, ImageDraw

        self.img = Image.new("RGB", (W, H), (246, 243, 232))
        self.d = ImageDraw.Draw(self.img)
        self.y = 40
        self.d.text((40, self.y), title, font=_font(38), fill=(30, 30, 30))
        self.y += 60
        self.d.line((40, self.y, W - 40, self.y), fill=(120, 110, 90), width=2)
        self.y += 20

    def line(self, text: str, size: int = 26, mono: bool = False, color=(30, 30, 30), gap: int = 10) -> bool:
        """Add a line; return False if the page is full."""
        font = _font(size, mono)
        for chunk in _wrap(self.d, text, font, W - 80):
            if self.y + size > H - 40:
                return False
            self.d.text((40, self.y), chunk, font=font, fill=color)
            self.y += size + gap
        return True


    def row(self, cols: list[str], xs: list[int], size: int = 26, color=(30, 30, 30), right: set[int] = frozenset()
            ) -> bool:
        """Table row: each column at a fixed x position (columns in right are right-aligned, x = right edge)."""
        if self.y + size > H - 40:
            return False
        font = _font(size)
        for i, (text, x) in enumerate(zip(cols, xs)):
            if i in right:
                x -= self.d.textlength(text, font=font)
            self.d.text((x, self.y), text, font=font, fill=color)
        self.y += size + 10
        return True


def _wrap(d, text: str, font, width: int) -> list[str]:
    out, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=font) <= width or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = word
    return out + ([cur] if cur else [])


def render(mission: MissionInfo | None, notes: list[dict]) -> list:
    pages = []
    if mission is not None:
        p = Page(spoken("Mission: {name}", name=mission.name)[:40])
        p.line(spoken("Aircraft: {aircraft} · Group: {group}", aircraft=mission.aircraft, group=mission.group))
        if mission.frequency_mhz:
            p.line(spoken("Group frequency: {mhz} MHz", mhz=f"{mission.frequency_mhz:.3f}"), size=30)
        p.y += 10
        xs, right = [40, 90, 470, 600, 728], {2, 3, 4}
        head = ["#", spoken("Name"), spoken("Altitude"), spoken("Distance"), spoken("Bearing")]
        p.row(head, xs, size=22, color=(90, 80, 60), right=right)
        for i, (wp, dist, brg) in enumerate(mission.legs()):
            cols = [str(i), wp.name[:18], f"{wp.alt_ft:,.0f} ft", f"{dist:.1f} nm" if i else "", f"{brg:03.0f}°" if i else ""]
            if not p.row(cols, xs, right=right):
                pages.append(p.img)
                p = Page(spoken("Waypoints (continued)"))
                p.row(head, xs, size=22, color=(90, 80, 60), right=right)
                p.row(cols, xs, right=right)
        p.line(spoken("Bearings are true (not magnetic), approximate."), size=18, color=(110, 100, 80))
        pages.append(p.img)
        if mission.description.strip():
            p = Page(spoken("Briefing"))
            for para in mission.description.splitlines():
                if para.strip() and not p.line(para.strip(), size=22):
                    break
            pages.append(p.img)
    if notes:
        p = Page(spoken("Notes"))
        for n in notes:
            if not p.line(f"{n.get('time', '')}  {n['text']}", size=26):
                pages.append(p.img)
                p = Page(spoken("Notes (continued)"))
                p.line(f"{n.get('time', '')}  {n['text']}", size=26)
        pages.append(p.img)
    return pages


def kneeboard_dirs(saved_games: list[Path]) -> list[Path]:
    # Pages in the root folder show up for every aircraft; writing to the aircraft folder too would duplicate them.
    return [d / "Kneeboard" for d in saved_games]


def write(pages: list, dirs: list[Path]) -> list[Path]:
    """Delete the old ALICE pages, then write the new ones."""
    written = []
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob(f"{PREFIX}*.png"):
            old.unlink()
        for i, img in enumerate(pages, 1):
            p = d / f"{PREFIX}{i:02d}.png"
            img.save(p)
            written.append(p)
    return written
