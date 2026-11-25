"""Convert screen into a numbered list of selectable elements for Jev: accessibility tree + OCR, with position and context.

Jev is still weak at interpreting the screen by itself (images, Box); the best-working Jev computer-use projects
(typesafe-computer-use, otto, CUA-JEV) read the screen deterministically and describe each element in a single line:
`button 'Save' (top-right)`, `text 'Fake defense' (left-top; in the row of '14 Oct')`. Here:

- controls from the accessibility tree (UIA / DOM) and on-screen text (OCR) are merged; if both are the same,
  it becomes a single item (`ax+ocr`), so two choices don't split votes;
- position is written using one-third screen regions (not numbers: Jev cannot compare numbers);
- row context is added to items with the same name (which one it is can only be understood from the row);
- texts repeating the goal are dropped: since the user's message "click the first project in projects" appears on screen,
  Jev was clicking that text;
- the list is limited so that items related to the goal come first.

OCR is done using Windows' built-in engine (winrt-Windows.Media.Ocr; user's languages including Turkish), and with
Tesseract on Linux and macOS. If neither is available, it works with the accessibility tree only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable

_WORD = re.compile(r"[\wçğıöşüÇĞİÖŞÜ]{2,}", re.U)
MAX_ITEMS = 60
ECHO_SHARE = 0.6  # if this fraction of words in the text appears in the goal, it's an echo of the goal (user's message)
ECHO_MIN_WORDS = 4  # shorter ones are button names: goal "Start new game" while "start a new game" was being dropped
# Between double quotes (straight, curly, angled). Not single quotes: Turkish uses them for suffixes ("WhatsApp'ta").
QUOTED_RX = re.compile(r'"[^"]*"|“[^”]*”|«[^»]*»|„[^“”]*[“”]')


def place(x: float, y: float) -> str:
    """Fractional point -> 'top-left' / 'center' (Jev cannot compare numbers; one-third regions)."""
    col = "left" if x < 1 / 3 else "right" if x > 2 / 3 else "middle"
    row = "top" if y < 1 / 3 else "bottom" if y > 2 / 3 else "middle"
    return "center" if col == row == "middle" else f"{row}-{col}"


def changed_places(prev: Any, cur: Any) -> list[str]:
    """Locations of changed regions, readable: let the model also see un-texted changes (card opened, unit walked).
    In Solitaire, when the evidence only said 'clicked seen location', the model hallucinated wrong lessons like 'you got lost in menus'."""
    regions = changed_regions(prev, cur) or []
    out: list[str] = []
    for l, t, r, b in regions:
        p = place((l + r) / 2, (t + b) / 2)
        if p not in out:
            out.append(p)
    return out


def words(s: str) -> list[str]:
    # I first: "ALİCE".lower() becomes "ali̇ce" (combined dot) and wouldn't match "alice".
    return [w.replace("İ", "i").replace("I", "i").lower().replace("ı", "i") for w in _WORD.findall(s or "")]


@dataclass
class Item:
    """A single selectable item on screen. rect: 0-1 fractional within window (left, top, right, bottom)."""

    key: str
    source: str  # ax | ocr | ax+ocr | dom
    role: str
    label: str
    rect: tuple[float, float, float, float] | None = None
    value: str = ""
    editable: bool = False
    context: str = ""
    ref: Any = None  # handle used by observer for action (UIA element number, DOM number)
    fresh: bool = False  # was not in previous observation (in games: newly appeared in this step/turn)

    @property
    def center(self) -> tuple[float, float] | None:
        if self.rect is None:
            return None
        l, t, r, b = self.rect
        return (l + r) / 2, (t + b) / 2

    def where(self) -> str:
        c = self.center
        return "" if c is None else place(*c)

    def describe(self, mark_new: bool = True) -> str:
        """mark_new=False: for past rows (so the same action doesn't look different due to 'new' flag)."""
        kind = "text" if self.source == "ocr" else (self.role or "control")
        s = f"{kind} '{self.label[:80]}'"
        if self.value:
            s += f" (value '{self.value[:40]}')"
        extra = "; ".join(x for x in (self.where(), self.context, "new" if self.fresh and mark_new else "") if x)
        return s + (f" ({extra})" if extra else "")


@dataclass
class Screen:
    """Result of an observation: window, items, and signature to understand changes."""

    title: str
    location: str
    items: list[Item] = field(default_factory=list)
    focused: str = ""
    image: Any = None  # PIL image (for visual fallback), otherwise None

    def signature(self) -> str:
        """Did the screen contents change? (item names and rough locations)"""
        parts = [self.title] + [f"{i.label}|{i.where()}|{i.value}" for i in self.items]
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def is_goal_echo(text: str, goal: str, echoes: tuple[str, ...] = ()) -> bool:
    """Is the on-screen text the goal itself (user message appearing in chat window)? echoes: other sources like user's
    original message; when the brain model rewrote the goal, the message on screen overlapped halfway with the goal."""
    tw = words(text)
    if len(tw) < ECHO_MIN_WORDS:
        return False
    for src in (goal, *echoes):
        # Inside quotes, it's the name of the thing to click/type on screen, not an echo: when the advisor said
        # 'Click the "Search or start a new Chat" box', WhatsApp's search box was counted as an echo and dropped,
        # and the reflex clicked 'Chats'.
        gw = set(words(QUOTED_RX.sub(" ", src or "")))
        if gw and sum(w in gw for w in tw) / len(tw) >= ECHO_SHARE:
            return True
    return False


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """What fraction of a is inside b (0-1)."""
    l, t = max(a[0], b[0]), max(a[1], b[1])
    r, bt = min(a[2], b[2]), min(a[3], b[3])
    if r <= l or bt <= t:
        return 0.0
    area = (a[2] - a[0]) * (a[3] - a[1])
    return ((r - l) * (bt - t)) / area if area > 0 else 0.0


def build_items(ax: list[Item], ocr: list[tuple[str, tuple[float, float, float, float]]], goal: str,
                limit: int = MAX_ITEMS, echoes: tuple[str, ...] = (), drop_echo: bool = True) -> list[Item]:
    """Accessibility items + OCR lines -> numbered, de-duplicated, goal-ordered list."""
    items: list[Item] = []
    for i in ax:
        if not (i.label or i.editable):
            continue
        # The same control can appear twice nested in the tree (Explorer's two 'Address Bar's): two options split the votes.
        if i.rect and any(o.role == i.role and o.label.strip().lower() == i.label.strip().lower() and o.rect
                          and _overlap(i.rect, o.rect) > 0.8 and _overlap(o.rect, i.rect) > 0.8 for o in items):
            continue
        items.append(i)
    for text, rect in ocr:
        text = " ".join(text.split())
        if not text or (drop_echo and is_goal_echo(text, goal, echoes)):
            continue
        # If the OCR line is inside a control and has the same text, don't make it a separate option: control becomes "ax+ocr".
        host = next((i for i in items if i.rect and i.source != "ocr" and _overlap(rect, i.rect) > 0.6
                     and (not i.label or set(words(text)) & set(words(i.label)))), None)
        if host is not None:
            if not host.label:
                host.label = text
            host.source = "ax+ocr" if host.source == "ax" else host.source
            continue
        items.append(Item("", "ocr", "text", text, rect))
    # If the same name is in multiple places, make its row understandable.
    by_label: dict[str, list[Item]] = {}
    for i in items:
        by_label.setdefault(i.label.strip().lower(), []).append(i)
    for same in by_label.values():
        if len(same) < 2:
            continue
        for i in same:
            if i.rect is None:
                continue
            # The element covering the window (WebView's Document) would become the "neighbor" of every row.
            row = [o.label for o in items if o is not i and o.rect and o.rect[3] - o.rect[1] < 0.1
                   and o.label.strip().lower() != i.label.strip().lower()
                   and abs(((o.rect[1] + o.rect[3]) / 2) - ((i.rect[1] + i.rect[3]) / 2)) < 0.02][:2]
            if row:
                i.context = "in the row of " + ", ".join(f"'{r[:30]}'" for r in row)
    # Items related to the goal first, then reading order (top to bottom, left to right).
    gw = set(words(goal))

    def order(i: Item) -> tuple:
        hit = len(gw & set(words(i.label + " " + i.value)))
        c = i.center or (0.5, 0.5)
        return (-hit, -int(i.editable), round(c[1], 2), c[0])

    items.sort(key=order)
    items = items[:limit]
    for n, i in enumerate(items, 1):
        i.key = f"i{n}"
    return items


# ------------------------------------------------------------------------------------------------ OCR
_ocr_engine: Any = None
_ocr_error = ""


def ocr_available() -> tuple[bool, str]:
    """(whether it is usable, and if not, why)"""
    try:
        _engine()
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _engine() -> Any:
    global _ocr_engine, _ocr_error
    if _ocr_engine is not None:
        return _ocr_engine
    if _ocr_error:
        raise RuntimeError(_ocr_error)
    import sys

    if sys.platform != "win32":
        import shutil

        exe = shutil.which("tesseract")
        if not exe:
            _ocr_error = ("screen reading (OCR) needs Tesseract outside Windows: sudo apt install tesseract-ocr "
                          "tesseract-ocr-tur (macOS: brew install tesseract tesseract-lang)")
            raise RuntimeError(_ocr_error)
        _ocr_engine = TesseractEngine(exe)
        return _ocr_engine
    try:
        from winrt.windows.media.ocr import OcrEngine
    except ImportError:
        from alice.deps import install_command

        _ocr_error = f"the OCR package isn't installed in this Python (ALICE installs it at startup), by hand: {install_command([])}"
        raise RuntimeError(_ocr_error) from None
    eng = OcrEngine.try_create_from_user_profile_languages()
    if eng is None:
        _ocr_error = "Windows has no OCR language (Settings → Language → add a language pack)"
        raise RuntimeError(_ocr_error)
    _ocr_engine = eng
    return eng


def ocr_image(img: Any, recognize: Callable[[Any], list[tuple[str, tuple[int, int, int, int]]]] | None = None
              ) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Text lines in the image with their fractional boxes. recognize is substituted in tests."""
    w, h = img.size
    lines = (recognize or _recognize_windows)(img)
    out = []
    for text, (x, y, rw, rh) in lines:
        out.append((text, (x / w, y / h, min(1.0, (x + rw) / w), min(1.0, (y + rh) / h))))
    return out


def ocr_words(img: Any) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Individual words in the image and their pixel boxes (x, y, width, height). Each marker on the same line, such as
    "M29 F15 MSL" on the RWR, needs its own position (the missile's direction comes from the position of MSL)."""
    eng = _engine()
    if isinstance(eng, TesseractEngine):
        return eng.words(img)
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    rgba = img.convert("RGBA")
    writer = DataWriter()
    writer.write_bytes(rgba.tobytes())
    bmp = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.RGBA8, rgba.width,
                                                 rgba.height, BitmapAlphaMode.PREMULTIPLIED)
    result = _wait(eng.recognize_async(bmp))
    return [(wd.text, (int(wd.bounding_rect.x), int(wd.bounding_rect.y), int(wd.bounding_rect.width),
                       int(wd.bounding_rect.height))) for line in result.lines for wd in line.words]


class TesseractEngine:
    """OCR outside Windows: the tesseract command (TSV output), English plus Turkish when that language is installed."""

    def __init__(self, exe: str, run: Callable[..., Any] | None = None):
        import subprocess

        self.exe = exe
        self.run = run or subprocess.run
        self._langs: str | None = None

    def langs(self) -> str:
        if self._langs is None:
            try:
                r = self.run([self.exe, "--list-langs"], capture_output=True, text=True, timeout=10)
                have = set(r.stdout.split())
            except Exception:  # noqa: BLE001 - asking for the list must not stop OCR; English is always there
                have = set()
            self._langs = "+".join(lang for lang in ("eng", "tur") if lang in have) or "eng"
        return self._langs

    def tsv(self, img: Any) -> str:
        import io

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        r = self.run([self.exe, "stdin", "stdout", "-l", self.langs(), "tsv"], input=buf.getvalue(),
                     capture_output=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"tesseract failed: {(r.stderr or b'').decode(errors='replace')[:200]}")
        return r.stdout.decode("utf-8", errors="replace")

    @staticmethod
    def parse(tsv: str) -> list[tuple[tuple[int, int, int], str, tuple[int, int, int, int]]]:
        """Words from tesseract's TSV: ((block, paragraph, line), text, (x, y, width, height))."""
        out = []
        for row in tsv.splitlines()[1:]:
            f = row.split("\t")
            if len(f) < 12 or f[0] != "5" or not f[11].strip():
                continue
            out.append(((int(f[2]), int(f[3]), int(f[4])), f[11].strip(),
                        (int(f[6]), int(f[7]), int(f[8]), int(f[9]))))
        return out

    def words(self, img: Any) -> list[tuple[str, tuple[int, int, int, int]]]:
        return [(text, box) for _line, text, box in self.parse(self.tsv(img))]

    def lines(self, img: Any) -> list[tuple[str, tuple[int, int, int, int]]]:
        groups: dict[tuple[int, int, int], list[tuple[str, tuple[int, int, int, int]]]] = {}
        for key, text, box in self.parse(self.tsv(img)):
            groups.setdefault(key, []).append((text, box))
        out = []
        for ws in groups.values():
            x0 = min(b[0] for _t, b in ws)
            y0 = min(b[1] for _t, b in ws)
            x1 = max(b[0] + b[2] for _t, b in ws)
            y1 = max(b[1] + b[3] for _t, b in ws)
            out.append((" ".join(t for t, _b in ws), (x0, y0, x1 - x0, y1 - y0)))
        return out


def _wait(op: Any) -> Any:
    """Wait for WinRT IAsyncOperation. Not a coroutine, just awaitable; asyncio.run doesn't accept it. If the caller
    is already inside an event loop (core), asyncio.run crashes: then it is awaited in a separate thread."""
    import asyncio

    async def go() -> Any:
        return await op

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(go())
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, go()).result()


def _recognize_windows(img: Any) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Windows.Media.Ocr: line text and pixel box (x, y, width, height). The engine reads at most max_image_dimension pixel
    edge: larger (3440 width) window is scaled down, boxes are rescaled back. On Linux and macOS: Tesseract."""
    eng = _engine()
    if isinstance(eng, TesseractEngine):
        return eng.lines(img)
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    limit = int(getattr(OcrEngine, "max_image_dimension", 0) or 0) or 4000  # static: not on the instance
    scale = min(1.0, limit / max(img.size))
    src = img if scale >= 1.0 else img.resize((int(img.width * scale), int(img.height * scale)))
    rgba = src.convert("RGBA")
    writer = DataWriter()
    writer.write_bytes(rgba.tobytes())
    bmp = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.RGBA8, rgba.width,
                                                 rgba.height, BitmapAlphaMode.PREMULTIPLIED)
    result = _wait(eng.recognize_async(bmp))
    out = []
    for line in result.lines:
        rects = [wd.bounding_rect for wd in line.words]
        if not rects:
            continue
        x0 = min(r.x for r in rects)
        y0 = min(r.y for r in rects)
        x1 = max(r.x + r.width for r in rects)
        y1 = max(r.y + r.height for r in rects)
        out.append((line.text, (int(x0 / scale), int(y0 / scale), int((x1 - x0) / scale), int((y1 - y0) / scale))))
    return out


# ------------------------------------------------------------------------------- only the changed parts (games)
Rect = tuple[float, float, float, float]
DIFF_GRID = (16, 9)  # the screen is split into this many cells
DIFF_PIXEL = 30  # a pixel that differs by this much in gray tone counts as "changed"
DIFF_CELL = 0.004  # the cell counts as changed if this fraction of it changed (also catches a single number changing)
FULL_OCR_SHARE = 0.5  # if more than half the screen changed, everything is read again from the start


def changed_regions(prev: Any, cur: Any, grid: tuple[int, int] = DIFF_GRID) -> list[Rect] | None:
    """Regions that changed between two frames (fractional boxes). None: incomparable, everywhere considered new.

    The screen is divided into a grid; changing neighbor cells merge into a single box. In games, after a turn, most of the map
    remains the same: only the newly popped-up window, changing resource counts, or moving units are re-read."""
    from PIL import Image, ImageChops

    if prev is None or cur is None or prev.size != cur.size:
        return None
    diff = ImageChops.difference(prev.convert("L"), cur.convert("L")).point(lambda v: 255 if v > DIFF_PIXEL else 0)
    gw, gh = grid
    cells = diff.resize((gw, gh), Image.BOX).load()
    hot = {(x, y) for y in range(gh) for x in range(gw) if cells[x, y] > 255 * DIFF_CELL}
    out: list[Rect] = []
    while hot:
        stack, comp = [hot.pop()], []
        while stack:
            x, y = stack.pop()
            comp.append((x, y))
            for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if n in hot:
                    hot.remove(n)
                    stack.append(n)
        xs, ys = [c[0] for c in comp], [c[1] for c in comp]
        out.append((min(xs) / gw, min(ys) / gh, (max(xs) + 1) / gw, (max(ys) + 1) / gh))
    return sorted(out, key=lambda r: (r[1], r[0]))


IMAGE_CHANGE = 0.001  # screen counts as changed if this fraction of the frame changed (blinking cursor ~0.3%, card ~30%)


def image_changed(prev: Any, cur: Any) -> bool:
    """Did the image change significantly? Text signature doesn't see un-texted changes: in Solitaire, clicking the deck
    opened a card, but because OCR couldn't read the card face, it said "screen did not change", stopping repeat protection."""
    if prev is None or cur is None:
        return False
    if prev.size != cur.size:
        return True
    from PIL import ImageChops

    diff = ImageChops.difference(prev.convert("L"), cur.convert("L")).point(lambda v: 255 if v > DIFF_PIXEL else 0)
    return diff.histogram()[255] / (cur.width * cur.height) >= IMAGE_CHANGE


CROP_MAX_SHARE = 0.5  # if the changed region is over half the screen, cropping is pointless; compare the full screen


def changed_crops(prev: Any, cur: Any) -> tuple[Any, Any] | None:
    """Before/after crops of changed regions (with cell share). Full screen goes downscaled to Jev; in a 3440-pixel
    window, the card number shrank to a few pixels. Like Evidence-First Reflection: first find where it changed,
    look closely there. None if no change or too large."""
    regions = changed_regions(prev, cur)
    if not regions:
        return None
    # Not union but largest region: in Solitaire the clock in the top right changes every minute; union stretched
    # from the deck to the clock and exceeded half the screen, canceling cropping.
    big = max(regions, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
    gw, gh = DIFF_GRID
    l, t = max(0.0, big[0] - 1 / gw), max(0.0, big[1] - 1 / gh)
    r_, b = min(1.0, big[2] + 1 / gw), min(1.0, big[3] + 1 / gh)
    if (r_ - l) * (b - t) > CROP_MAX_SHARE:
        return None
    w, h = cur.size
    box = (int(l * w), int(t * h), int(r_ * w), int(b * h))
    return prev.crop(box), cur.crop(box)


def image_key(img: Any) -> str:
    """Rough fingerprint of the image (64x36 gray, 8 tones). Loop protection also recognizes returning to the same screen with this:
    don't consider it a loop on a screen where the text stays the same but the image changes (drawing cards from deck successively)."""
    if img is None:
        return ""
    small = img.convert("L").resize((64, 36)).point(lambda v: v // 32)
    return hashlib.sha1(small.tobytes()).hexdigest()[:12]


def _touches(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def ocr_changed(img: Any, prev_lines: list[tuple[str, Rect]], regions: list[Rect] | None,
                recognize: Callable[[Any], list[tuple[str, tuple[int, int, int, int]]]] | None = None
                ) -> list[tuple[str, Rect]]:
    """Unchanged items of previous lines + re-reading of changed regions. Old lines touching the region also join
    the region (so a half-changed line isn't half-read)."""
    if regions is None or sum((r[2] - r[0]) * (r[3] - r[1]) for r in regions) > FULL_OCR_SHARE:
        return ocr_image(img, recognize)
    keep, grown = list(prev_lines), []
    for reg in regions:
        hit = [ln for ln in keep if _touches(ln[1], reg)]
        keep = [ln for ln in keep if ln not in hit]
        grown.append((min([reg[0]] + [h[1][0] for h in hit]), min([reg[1]] + [h[1][1] for h in hit]),
                      max([reg[2]] + [h[1][2] for h in hit]), max([reg[3]] + [h[1][3] for h in hit])))
    w, h = img.size
    out = keep
    for l, t, r, b in grown:
        box = (int(l * w), int(t * h), max(int(r * w), int(l * w) + 40), max(int(b * h), int(t * h) + 40))
        crop = img.crop(box)
        cw, ch = crop.size
        for text, (cl, ct, cr, cb) in ocr_image(crop, recognize):
            out.append((text, ((box[0] + cl * cw) / w, (box[1] + ct * ch) / h, min(1.0, (box[0] + cr * cw) / w),
                               min(1.0, (box[1] + cb * ch) / h))))
    return out


CHANGES_MAX = 12  # at most this many texts per direction


def screen_changes(before: list[Item], after: list[Item]) -> dict[str, list[str]]:
    """Appeared and disappeared texts between two screens (proof known for sure by code). Values like turn counter, year, gold
    appear here no matter what game it is: 'Turn 3' disappears, 'Turn 4' appears."""
    def labels(items: list[Item]) -> dict[str, str]:
        out: dict[str, str] = {}
        for i in items:
            text = " ".join(i.label.split())
            if len(text) >= 2:
                out.setdefault(text.lower(), text)
        return out

    a, b = labels(before), labels(after)
    return {"appeared": [b[k] for k in b if k not in a][:CHANGES_MAX],
            "disappeared": [a[k] for k in a if k not in b][:CHANGES_MAX]}


def mark_fresh(items: list[Item], before: set[str] | None) -> set[str]:
    """Mark items not in previous observation as 'new'; return the item set of this observation. before None: first observation."""
    now = {f"{i.role}|{i.label.strip().lower()}" for i in items}
    if before is not None:
        for i in items:
            i.fresh = f"{i.role}|{i.label.strip().lower()}" not in before
    return now
