"""Patterns of "AI-made" look in website design: a code-based audit and a critique from screenshots.

Models keep drifting into the same patterns when designing (mono labels like "AETH-SYS // BRİFİNG-01", "> İNCELE"
buttons, "128.400+" counter lines, neon glows, "Geleceği şekillendiriyoruz" slogans). If the board and agents do not
notice these by eye, the site becomes "the most AI slop site in the world". The audit here does not depend on the model:
the patterns are counted from the files and sent to the agents and the board as concrete "this file has this, fix it
this way".
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".alice", "dist", "build"}
MAX_FILES = 200
MAX_BYTES = 400_000


@dataclass
class Hit:
    rule: str
    title: str
    fix: str
    count: int = 0
    examples: list[str] = field(default_factory=list)

    def add(self, where: str, sample: str) -> None:
        self.count += 1
        sample = re.sub(r"\s+", " ", sample).strip()[:80]
        item = f"{where}: {sample}" if sample else where
        if len(self.examples) < 4 and item not in self.examples:
            self.examples.append(item)


# (rule, title, fix, minimum number of occurrences before it counts as a problem)
RULES: dict[str, tuple[str, str, int]] = {
    "slash_labels": ("Code/terminal-style decorative labels (\"AETH-SYS // BRIEFING-01\", \"SEC-02 // ...\")",
                     "Remove the labels above sections, or make them a plain small heading (\"Platforms\"). '//' and code "
                     "numbers don't look technical, they look like a template.", 2),
    "prompt_buttons": ("Button/link text starts with \">\", \"›\", \"→\" (\"> EXPLORE PLATFORMS\")",
                       "Make the button text a plain verb (\"Explore platforms\"); if an arrow is needed, a small icon at "
                       "the end of the text.", 1),
    "stat_counters": ("Counter/statistics row (\"128,400+\", \"99.2%\", \"640+\")",
                      "A strip of four statistics is the best-known AI pattern. Remove it, or use a single number "
                      "inside a sentence, with its context.", 3),
    "fake_annotations": ("Fake technical notes (\"FIG. 01\", \"DRAWING NO\", \"REV: D\", \"BRIEFING\")",
                         "Remove made-up technical labels used as decoration; write a caption that actually informs "
                         "next to the image, or write nothing.", 2),
    "glow": ("Neon glow shadows (0 0 Npx box/text-shadow, drop-shadow)",
             "Remove the glow effects; if depth is needed, a soft, colourless, short shadow, or none.", 2),
    "gradient_text": ("Gradient text (background-clip: text)",
                      "Write headings in a solid colour; give emphasis with size, weight and spacing.", 1),
    "glass": ("Frosted glass effect (backdrop-filter: blur) in many places",
              "Frosted glass at most on a fixed top menu; remove it from cards and sections.", 2),
    "techno_bg": ("Grid/scan-line/radar/particle backgrounds",
                  "Remove decorative backgrounds; use a plain surface, a strong photo or typography.", 2),
    "uppercase": ("UPPERCASE with wide letter spacing everywhere",
                  "Limit uppercase + letter spacing to 1-2 small places; most text should be written normally.", 10),
    "mono_font": ("Monospace font everywhere in the interface",
                  "A mono font only for real code/data; a normal font for headings, labels and buttons.", 5),
    "buzzwords": ("Empty slogans and cliché words (\"shaping the future\", \"superiority\", \"next-gen\")",
                  "Replace slogans with concrete sentences: who, what they do, what they do differently (a fact, a "
                  "number, an example).", 3),
    "emoji": ("Emoji instead of icons", "Remove the emoji; if needed, consistent thin-line SVG icons.", 2),
    "ticker": ("Scrolling text / typewriter effect / blinking cursor",
               "Remove scrolling bands and typewriter effects; let the text stay still.", 1),
}

_SLASH = re.compile(r"(?<![:/\w])[A-ZÇĞİÖŞÜ0-9][A-ZÇĞİÖŞÜ0-9.\-_]{1,}\s+//\s+[A-ZÇĞİÖŞÜ0-9]", re.U)
_PROMPT_BTN = re.compile(r"<(?:a|button)\b[^>]*>\s*(?:<[^>]+>\s*)*(?:&gt;|>|›|&rsaquo;|→|&rarr;|»|&raquo;)\s",
                         re.I | re.S)
_STAT = re.compile(r"(?<![\w.,])(?:\d{1,3}(?:[.,]\d{3})+|\d{2,})\s*\+(?!\w)|%\s?\d{1,3}[.,]\d|\b\d{1,3}[.,]\d\s?%")
_COUNTER_JS = re.compile(r"data-(?:count|target|counter|to)\b|countUp|animateCount|counter-?anim", re.I)
_FAKE = re.compile(r"ŞEK[İI]L\s*\d|\bFIG\.?\s*\d|ÇİZİM\s*NO|\bREV\s*[:.]\s*[A-Z0-9]|BR[İI]F[İI]NG|\bSCH-\d|"
                   r"KOORD[İI]NAT|LAT\s*\d+[.,]\d+|\bN\s?\d{2}°", re.I)
_GLOW = re.compile(r"(?:box|text)-shadow\s*:[^;{}]*\b0(?:px)?\s+0(?:px)?\s+\d{2,}px|drop-shadow\(\s*0(?:px)?\s+0(?:px)?\s+\d",
                   re.I)
_GRAD_TEXT = re.compile(r"background-clip\s*:\s*text", re.I)
_GLASS = re.compile(r"backdrop-filter\s*:\s*blur", re.I)
_TECHNO = re.compile(r"repeating-(?:linear|radial)-gradient|scan-?line|radar|grid-overlay|noise-overlay|particles?\b|"
                     r"matrix-rain|crt\b", re.I)
_UPPER = re.compile(r"text-transform\s*:\s*uppercase", re.I)
_MONO = re.compile(r"font-family\s*:[^;{}]*(?:monospace|\bmono\b|courier|consolas)", re.I)
_TICKER = re.compile(r"<marquee|class=\"[^\"]*\b(?:ticker|marquee|typewriter|typing)\b|@keyframes\s+(?:blink|ticker|"
                     r"marquee|typing|scroll-x)", re.I)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
BUZZWORDS = ("geleceği", "üstünlük", "tavizsiz", "yeni nesil", "son teknoloji", "çığır", "inovasyon", "inovatif",
             "sınırları zorla", "kusursuz", "eşsiz", "dünya standartlarında", "mükemmellik", "vizyoner",
             "yenilikçi çözümler", "uçtan uca", "next-gen", "cutting-edge", "unmatched", "seamless")


def _around(text: str, m: re.Match, before: int = 18, after: int = 22) -> str:
    """Context around the match, without cutting through a word."""
    a, b = max(0, m.start() - before), min(len(text), m.end() + after)
    while a > 0 and not text[a - 1].isspace() and m.start() - a < before + 15:
        a -= 1
    while b < len(text) and not text[b].isspace() and b - m.end() < after + 15:
        b += 1
    return text[a:b]


def visible_text(raw: str) -> str:
    """Visible text from HTML (rough): script/style and tags are removed."""
    raw = re.sub(r"<(script|style|svg|noscript)\b.*?</\1>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<!--.*?-->", " ", raw, flags=re.S)
    raw = re.sub(r"<[^>]+>", "\n", raw)
    return htmllib.unescape(raw)


def _files(root: Path) -> list[Path]:
    out: list[Path] = []
    stack = [Path(root)]
    while stack and len(out) < MAX_FILES:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.name.startswith(".") or p.name in SKIP_DIRS:
                continue
            if p.is_dir():
                stack.append(p)
            elif p.suffix.lower() in (".html", ".htm", ".css", ".scss", ".js", ".jsx", ".tsx", ".vue", ".svelte"):
                out.append(p)
    return out


def lint(root: Path) -> list[Hit]:
    """Count the patterns on the site. Returns the rules over the threshold (most frequent first)."""
    hits = {k: Hit(k, t, f) for k, (t, f, _n) in RULES.items()}
    root = Path(root)
    counter_js = False
    for p in _files(root):
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        ext = p.suffix.lower()
        styles = raw
        if ext in (".html", ".htm", ".vue", ".svelte", ".jsx", ".tsx"):
            text = visible_text(raw)
            for m in _SLASH.finditer(text):
                hits["slash_labels"].add(rel, _around(text, m))
            for m in _PROMPT_BTN.finditer(raw):
                hits["prompt_buttons"].add(rel, visible_text(raw[m.start():m.end() + 40]))
            for m in _STAT.finditer(text):
                hits["stat_counters"].add(rel, _around(text, m))
            for m in _FAKE.finditer(text):
                hits["fake_annotations"].add(rel, _around(text, m))
            low = text.lower()
            for w in BUZZWORDS:
                for m in re.finditer(re.escape(w), low):
                    hits["buzzwords"].add(rel, _around(text, m))
            for m in _EMOJI.finditer(text):
                hits["emoji"].add(rel, _around(text, m))
            styles = " ".join(re.findall(r"<style\b[^>]*>(.*?)</style>", raw, re.S | re.I)) + " " + \
                " ".join(re.findall(r"style=\"([^\"]*)\"", raw))
        if ext in (".js", ".jsx", ".tsx", ".html", ".htm") and _COUNTER_JS.search(raw):
            counter_js = True
        if ext == ".js":
            styles = ""
        for rx, key in ((_GLOW, "glow"), (_GRAD_TEXT, "gradient_text"), (_GLASS, "glass"), (_TECHNO, "techno_bg"),
                        (_UPPER, "uppercase"), (_MONO, "mono_font")):
            for m in rx.finditer(styles):
                hits[key].add(rel, styles[m.start():m.end() + 10])
        for m in _TICKER.finditer(raw):
            hits["ticker"].add(rel, raw[m.start():m.end()])
    if counter_js and hits["stat_counters"].count:
        hits["stat_counters"].count += 2  # if there is also a counter animation, it is a sure pattern
    found = [h for k, h in hits.items() if h.count >= RULES[k][2]]
    return sorted(found, key=lambda h: -h.count)


def report(hits: list[Hit], limit: int = 8) -> str:
    """Short report for the agents/board. Empty if there is no problem."""
    if not hits:
        return ""
    rows = []
    for h in hits[:limit]:
        ex = "; ".join(h.examples[:3])
        rows.append(f"- {h.title} — in {h.count} places. Example: {ex}\n  Fix: {h.fix}")
    return "\n".join(rows)


def as_dicts(hits: list[Hit]) -> list[dict[str, Any]]:
    return [{"rule": h.rule, "title": h.title, "count": h.count, "examples": h.examples, "fix": h.fix} for h in hits]


CRITIC_PROMPT = """You are an experienced art director. Attached are screenshots of a website ({views}).
Project: {goal}
{design}
Your job: does this site look like it came from a real, good design studio, or like a template made with AI? Be
honest and harsh; don't call it good out of politeness.

These patterns are signs of AI: code/terminal decoration labels ("XX // YY"), buttons starting with ">", counter rows
("128.400+"), fake technical notes ("ŞEKİL 01"), neon glows, gradient text, a single neon accent on dark navy,
uppercase and mono type everywhere, empty slogans, small drawings in boxes, the same card grid in every section,
abstract decoration instead of photos.

Write in Turkish, briefly:
1. First impression (one sentence) and a score from 1-10 for: originality, visual hierarchy, typography,
   spacing/layout.
2. The 5 most important problems: for each, in which screenshot and where, why it is bad, and concretely how to fix
   it (color, size, placement, content). Not general advice: instructions that can be applied.
3. 1-2 good things worth keeping (if any)."""


STUDY_PROMPT = """You are an experienced art director doing reference research for a design team.
Attached are screenshots of a real site: {url} ({views}; "uzun" (long) also shows the lower part of the page).
Ignore any cookie banner or pop-up.
Our project: {goal}
{note}
Break down this site's design in concrete, measurable terms (write observations, not adjectives). In Turkish, short
bullet points:
1. Overall feel and who it speaks to (one sentence).
2. Layout: grid, margins, what is on the first screen, how the sections are ordered, spacing rhythm (roughly how many
   px between sections), symmetry/asymmetry.
3. Typography: heading typeface kind (serif/sans/grotesque, narrow/wide), weight, approximate size (px) and line
   height of the main heading, body text size, use of uppercase, heading length (how many words).
4. Color: background, text and accent colors (approximate hex) and their proportions (e.g. 80% white, 15% dark, 5%
   accent).
5. Visual language: photos or illustrations, framing, what they show; any decoration.
6. Tone of the copy: write the main heading and one subheading exactly; how it speaks.
7. 3 things it does well: how to apply them to our site (concrete instructions).
8. 1 thing to avoid (if any)."""


def _vision_chat(router, text: str, images: list[str], max_tokens: int = 1500) -> str:
    """One-off question to a model that can see images. Empty if there is no model or on error."""
    if not images or router is None:
        return ""
    from alice.providers import picker

    try:
        chain = picker.chain_for(router, "vision")
    except Exception:  # noqa: BLE001
        return ""
    if not chain:
        return ""
    role = f"design-vision-{id(text) % 100000}"
    router.role_overrides[role] = chain
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
    try:
        res = router.chat(role, [{"role": "user", "content": parts}], temperature=0.3, max_tokens=max_tokens)
    except Exception:  # noqa: BLE001 - no vision model or error
        return ""
    finally:
        router.role_overrides.pop(role, None)
    return (getattr(res, "text", "") or "").strip()


def study(router, shots: dict[str, str], url: str, goal: str, note: str = "") -> str:
    """Analyze the reference site's screenshots: layout, typography, color, visual language, takeaways."""
    text = STUDY_PROMPT.format(url=url, views=", ".join(shots), goal=goal[:600],
                               note=f"Look especially at: {note}\n" if note.strip() else "")
    return _vision_chat(router, text, list(shots.values()), max_tokens=1800)


def critique(router, shots: dict[str, str], goal: str, design: str = "", lint_text: str = "",
             refs: dict[str, str] | None = None) -> str:
    """Have a model that can see screenshots critique them. refs: screenshots of the reference sites from
    the research (for comparison, at most 2). Empty if there is no model or on error."""
    if not shots:
        return ""
    refs = dict(list((refs or {}).items())[:2])
    text = CRITIC_PROMPT.format(views=", ".join(shots), goal=goal[:600],
                                design=f"Summary of the design direction document:\n{design[:1500]}\n" if design.strip() else "")
    if refs:
        text += ("\n\nThe last screenshots are the reference sites the team took as examples (" + ", ".join(refs) + "). "
                 "Compare our site with them: is it the same quality, and what is the biggest difference?")
    if lint_text:
        text += f"\n\nPatterns found automatically in the code (check them in the screenshots too):\n{lint_text[:2000]}"
    return _vision_chat(router, text, list(shots.values()) + list(refs.values()))
