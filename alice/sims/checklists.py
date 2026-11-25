"""Spoken checklists.

Lists are markdown: the heading is the list's name, each "- " line is a step. First ~/.alice/checklists/
(your own lists) is searched, then the bundled ones (F-16C, simplified; checklists/en/ when the speech language is
English).
"Alice, iniş kontrol listesi" (landing checklist) -> the first step is read; "tamam" / "devam" -> the next step.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from alice.config import alice_home
from alice.i18n import speech_language, spoken

BUILTIN = Path(__file__).with_name("checklists")


def _norm(s: str) -> str:
    s = s.lower().replace("ı", "i")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


@dataclass
class Checklist:
    key: str
    title: str
    steps: list[str]


def load_all() -> dict[str, Checklist]:
    out: dict[str, Checklist] = {}
    builtin = BUILTIN / "en" if speech_language() == "en" and (BUILTIN / "en").is_dir() else BUILTIN
    for folder in (builtin, alice_home() / "checklists"):  # the user's own lists overwrite the built-in ones
        if not folder.is_dir():
            continue
        for p in sorted(folder.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
            title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), p.stem)
            steps = [ln[2:].strip() for ln in text.splitlines() if ln.startswith(("- ", "* "))]
            if steps:
                out[p.stem] = Checklist(p.stem, title, steps)
    return out


def find(name: str, lists: dict[str, Checklist] | None = None) -> Checklist | None:
    lists = lists if lists is not None else load_all()
    q = _norm(name).replace("kontrol listesi", "").replace("checklist", "").strip()
    best, score = None, 0
    for cl in lists.values():
        words = set(_norm(cl.title).split()) | set(_norm(cl.key).replace("-", " ").split())
        qs = set(q.split())
        s = len(words & qs) * 2 - len(words - qs) * 0.1
        if _norm(cl.key) == q or _norm(cl.title) == q:
            s += 10
        if s > score:
            best, score = cl, s
    return best


class Runner:
    """Keeps track of where we are in a list."""

    def __init__(self, cl: Checklist):
        self.cl = cl
        self.i = 0

    def current(self) -> str:
        if self.i >= len(self.cl.steps):
            return spoken("{title} checklist complete.", title=self.cl.title)
        return spoken("Step {n}/{total}: {step}", n=self.i + 1, total=len(self.cl.steps), step=self.cl.steps[self.i])

    def next(self) -> str:
        self.i = min(self.i + 1, len(self.cl.steps))
        return self.current()

    def prev(self) -> str:
        self.i = max(self.i - 1, 0)
        return self.current()

    @property
    def done(self) -> bool:
        return self.i >= len(self.cl.steps)
