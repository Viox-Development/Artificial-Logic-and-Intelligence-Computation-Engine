"""Look of the HUD. Follows Apple's Dynamic Island and Live Activities rules (developer.apple.com/design/human-
interface-guidelines/live-activities, /materials, /color):

  * One surface, "Island": a single capsule at the top center of the screen. Threats, fuel, temperature,
    notifications and speech all live inside it; if there is more than one thing, the most important one is in the
    Island and the others are small circles beside it (minimal view). There are no separate floating panels: HIG says
    not to stack translucent layers and to alternate content on a single surface.
  * Light language: white-gray frosted glass (the screen behind it is actually blurred, with a light veil on top, a
    hairline edge and light on the top edge). Colors are the same Graphite palette as the desktop and phone: text
    #18181B, accent blue #2F6BFF, glass white and background #F3F3F4. A black Island and saturated orange looked like
    a "game interface". Color is only on icons and values, and only for meaning; for critical, a thin red line and a
    slow breath. No colored dots, no colored borders.
  * Text is large and at least medium weight (HIG: "large, heavier-weight text"). Segoe UI Variable's Display/Text
    optical sizes are the closest Windows match to SF. Icons are single-color like SF Symbols, at the same weight as the
    text.
  * Concentric margins: content is tight to the edge, inner corners match the outer corner; corners are Apple's
    continuous corner (squircle, n=5 super-ellipse).
  * Motion: the Island grows and shrinks like a spring when its size changes; when content changes, the old one fades
    out and the new one fades in. No animation lasts longer than 2 s.

Besides the Island, only two things are drawn: a soft light on the screen edge in the direction of a critical threat,
and a macOS focus ring around the place to click. This module does not touch Windows; pressing the window is
window.py's job.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from alice.hud.model import Item, clock_of

# Colors are the same Graphite palette as the desktop and phone (alice/app/static/app.css :root): accent blue #2F6BFF,
# text #18181B, background #F3F3F4. There is one accent: info and warnings share the same blue, and only the icon and
# text tell them apart. Critical threats stay red for safety. (Old names are kept; the drawing code uses them.)
BRAND_BLUE = (47, 107, 255)      # #2F6BFF  --accent
SHARK = (24, 24, 27)             # #18181B  --text
ATHENS = (243, 243, 244)         # #F3F3F4  --side
RED = (217, 58, 58)              # #D93A3A  --err: critical only
BLUE, GRAY = BRAND_BLUE, (155, 155, 163)  # GRAY: --faint
ORANGE = GREEN = YELLOW = BRAND_BLUE  # old names: don't step outside the palette
GLOW = {"info": BRAND_BLUE, "warn": BRAND_BLUE, "critical": (240, 100, 100)}  # edge light
COLORS = {"info": BRAND_BLUE, "warn": BRAND_BLUE, "critical": RED}
LABEL = SHARK                    # main text
LABEL_2 = (*SHARK, 150)          # secondary text: same color at 59% (hierarchy without leaving the palette)
TINT = (255, 255, 255, 176)      # light veil of the glass: white, over the blurred screen
TINT_SOLID = (*ATHENS, 242)      # if the background can't be captured: Athens Gray
HAIRLINE = (*SHARK, 22)          # hairline separating the glass from the background
HIGHLIGHT = (255, 255, 255, 200)  # light reflection on the top edge (sense of glass thickness)
TRACKING = -0.022                # em: on apple.com, -0.374 px at 17 px text; tight, solid look
SS = 3                           # magnification for edge smoothing

FADE_IN, FADE_OUT, TURN = 0.22, 0.18, 0.2   # sec
PULSE = 1.6                                 # sec: critical breath (0.6 Hz: far below the limit of 3 flashes per second)
LISTEN_PULSE = 2.4                          # sec: glass shimmer while listening (slower than critical, calm)
WAVE_SPEED = 7.0                            # waveform phase / sec
PRIORITY = {"critical": 0, "warn": 1, "info": 2}


def scale_for(screen_h: int) -> float:
    """1 at 1080p; 1.33 at 1440p; 2 at 4K."""
    return max(1.0, min(2.0, screen_h / 1080))


# ------------------------------------------------------------------ text
@lru_cache(maxsize=64)
def font(size: int, weight: int = 600, display: bool = True) -> ImageFont.FreeTypeFont:
    """Segoe UI Variable: weight 300-700, optical size (display for big headings, text for body). Else Segoe UI."""
    for path in ("C:/Windows/Fonts/SegUIVar.ttf",):
        try:
            f = ImageFont.truetype(path, size)
            f.set_variation_by_axes([weight, 36 if display else 10])
            return f
        except (OSError, ValueError, AttributeError):
            continue
    for name in (("seguisb.ttf", "segoeuib.ttf") if weight >= 600 else ("segoeui.ttf",)) + ("arial.ttf",):
        try:
            return ImageFont.truetype("C:/Windows/Fonts/" + name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _tw(s: str, f: ImageFont.FreeTypeFont, tracking: float = TRACKING) -> int:
    """Text width including letter spacing."""
    return int(math.ceil(f.getlength(s) + tracking * f.size * max(0, len(s) - 1)))


def text(d: ImageDraw.ImageDraw, xy: tuple[float, float], s: str, f: ImageFont.FreeTypeFont, fill: tuple,
         anchor: str = "lm", tracking: float = TRACKING) -> None:
    """Negative letter-spaced text (PIL doesn't have it: drawn letter by letter, keeping each pair's own kerning).
    anchor: l/r/m horizontal + m/a vertical (lm, rm, la)."""
    if not s:
        return
    width = _tw(s, f, tracking)
    x, y = xy
    if anchor[0] == "r":
        x -= width
    elif anchor[0] == "m":
        x -= width / 2
    v = anchor[1]
    pos = x
    for i, ch in enumerate(s):
        d.text((pos, y), ch, font=f, fill=fill, anchor="l" + v)
        pair = f.getlength(s[i:i + 2]) - f.getlength(s[i + 1]) if i + 1 < len(s) else f.getlength(ch)
        pos += pair + tracking * f.size


def tr_sentence(s: str) -> str:
    """'ARKANDA · 900 m' -> 'Arkanda · 900 m' (with Turkish İ/ı). Shouting in all caps isn't Apple's style."""
    def low(w: str) -> str:
        return w.replace("I", "ı").replace("İ", "i").lower()

    parts = s.split(" ")
    out = []
    for p in parts:
        letters = [c for c in p if c.isalpha()]
        # abbreviations (SAM, GPU, DCS) stay; shouted words (ARKANDA, YAKIT) get lowercased
        out.append(low(p) if len(letters) > 3 and all(c.isupper() for c in letters) else p)
    t = " ".join(out)
    if t and t[0] == "i":
        return "İ" + t[1:]
    return t[:1].upper() + t[1:]


def _wrap(s: str, f: ImageFont.FreeTypeFont, width: int, max_lines: int = 3) -> list[str]:
    words, lines, cur = s.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if cur and _tw(t, f) > width:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _tw(last + "…", f) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


# ------------------------------------------------------------------ shape
def squircle(box: tuple[float, float, float, float], r: float, n: float = 5.0, steps: int = 14) -> list[tuple]:
    """Apple's continuous-corner rectangle: corners are super-ellipses (n=5), a softer transition than a
    circular corner. If r equals height/2 it becomes a capsule."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    r = max(0.0, min(r, w / 2, h / 2))
    if r < 0.5:
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if r >= min(w, h) / 2 - 0.5:  # capsule/circle: ends are full half circles (the super-ellipse looked boxy here)
        n, e = 2.0, min(w, h) / 2
    else:
        e = min(r * 1.28, w / 2, h / 2)  # super-ellipse corner, a bit longer for the same apparent radius
    pts = []
    for cx, cy, a0 in ((x1 - e, y0 + e, -90), (x1 - e, y1 - e, 0), (x0 + e, y1 - e, 90), (x0 + e, y0 + e, 180)):
        for i in range(steps + 1):
            a = math.radians(a0 + 90 * i / steps)
            ca, sa = math.cos(a), math.sin(a)
            px = math.copysign(abs(ca) ** (2 / n), ca) * e
            py = math.copysign(abs(sa) ** (2 / n), sa) * e
            pts.append((cx + px, cy + py))
    return pts


def _big(size: tuple[int, int]) -> Image.Image:
    return Image.new("RGBA", (size[0] * SS, size[1] * SS), (0, 0, 0, 0))


def _down(big: Image.Image, size: tuple[int, int]) -> Image.Image:
    return big.resize(size, Image.LANCZOS)


def _shape(img: Image.Image, box: tuple, r: float, fill: tuple, outline: tuple | None = None, width: float = 1.0) -> None:
    big = _big(img.size)
    pts = [(x * SS, y * SS) for x, y in squircle(box, r)]
    d = ImageDraw.Draw(big)
    d.polygon(pts, fill=fill)
    if outline:
        d.line(pts + [pts[0]], fill=outline, width=max(1, round(width * SS)), joint="curve")
    img.alpha_composite(_down(big, img.size))


def _shadow(img: Image.Image, box: tuple, r: float, k: float, alpha: int = 120) -> None:
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(sh).polygon(squircle((x0, y0 + 6 * k, x1, y1 + 6 * k), r), fill=(0, 0, 0, alpha))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(12 * k)))


# ------------------------------------------------------------------ icons (single color, like SF Symbols)
def glyph(img: Image.Image, name: str, cx: float, cy: float, s: float, rgb: tuple, angle: float = 0.0) -> None:
    """s: the icon's box size (as tall as the text). All are drawn at 3x and scaled down."""
    big = _big(img.size)
    d = ImageDraw.Draw(big)
    col = (*rgb, 255)
    X = lambda v: v * SS  # noqa: E731
    h = s / 2
    if name == "arrow":  # location.north.fill: filled arrow with a direction
        dx, dy = math.sin(math.radians(angle)), -math.cos(math.radians(angle))
        px, py = -dy, dx
        shape = ((0.0, -1.0), (0.56, 0.7), (0.0, 0.34), (-0.56, 0.7))  # tip kept clear of the wings: direction is obvious
        d.polygon([(X(cx + (px * a - dx * b) * h), X(cy + (py * a - dy * b) * h)) for a, b in shape], fill=col)
    elif name == "warning":  # exclamationmark.triangle.fill
        tri = [(cx, cy - h * 0.95), (cx + h, cy + h * 0.8), (cx - h, cy + h * 0.8)]
        d.polygon([(X(x), X(y)) for x, y in tri], fill=col)
        d.line([(X(cx), X(cy - h * 0.35)), (X(cx), X(cy + h * 0.25))], fill=(255, 255, 255, 255), width=round(X(s * 0.11)))
        r = s * 0.07
        d.ellipse([X(cx - r), X(cy + h * 0.5 - r), X(cx + r), X(cy + h * 0.5 + r)], fill=(255, 255, 255, 255))
    elif name == "drop":  # drop.fill (fuel)
        r = h * 0.62
        d.ellipse([X(cx - r), X(cy + h * 0.3 - r), X(cx + r), X(cy + h * 0.3 + r)], fill=col)
        d.polygon([(X(cx), X(cy - h)), (X(cx + r * 0.92), X(cy + h * 0.12)), (X(cx - r * 0.92), X(cy + h * 0.12))],
                  fill=col)
    elif name == "thermometer":
        w = s * 0.2
        d.rounded_rectangle([X(cx - w / 2), X(cy - h), X(cx + w / 2), X(cy + h * 0.4)], radius=X(w / 2), fill=col)
        r = s * 0.24
        d.ellipse([X(cx - r), X(cy + h * 0.55 - r), X(cx + r), X(cy + h * 0.55 + r)], fill=col)
    elif name == "check":  # checkmark.circle.fill
        d.ellipse([X(cx - h), X(cy - h), X(cx + h), X(cy + h)], fill=col)
        pts = [(cx - h * 0.45, cy + h * 0.02), (cx - h * 0.1, cy + h * 0.38), (cx + h * 0.5, cy - h * 0.35)]
        d.line([(X(x), X(y)) for x, y in pts], fill=(255, 255, 255, 255), width=round(X(s * 0.13)), joint="curve")
    elif name == "cursor":  # cursorarrow
        pts = [(0, -1), (0.0, 0.62), (0.3, 0.32), (0.55, 0.92), (0.72, 0.84), (0.48, 0.25), (0.88, 0.25)]
        d.polygon([(X(cx - h * 0.4 + a * h), X(cy + b * h)) for a, b in pts], fill=col)
    elif name == "radar":  # dot.radiowaves: threat of unknown direction (an arrow would point the wrong way)
        r = s * 0.13
        d.ellipse([X(cx - r), X(cy - r), X(cx + r), X(cy + r)], fill=col)
        for rr in (0.5, 0.9):
            for a0 in (-50, 130):
                d.arc([X(cx - h * rr), X(cy - h * rr), X(cx + h * rr), X(cy + h * rr)], a0, a0 + 100, fill=col,
                      width=round(X(s * 0.1)))
    elif name == "wave":  # waveform: ALICE is listening/speaking; angle is the phase here (drifts a little each frame)
        n, bw = 4, s * 0.13
        for i in range(n):
            amp = 0.3 + 0.7 * abs(math.sin(math.radians(angle) + i * 0.95))
            x = cx + (i - (n - 1) / 2) * s * 0.24
            d.rounded_rectangle([X(x - bw / 2), X(cy - h * amp), X(x + bw / 2), X(cy + h * amp)],
                                radius=X(bw / 2), fill=col)
    elif name == "music":  # music.note: the song currently playing
        w = round(X(s * 0.11))
        r = s * 0.17
        for hx in (-0.42, 0.42):
            d.ellipse([X(cx + hx * h - r - s * 0.08), X(cy + h * 0.62 - r), X(cx + hx * h + r - s * 0.08),
                       X(cy + h * 0.62 + r)], fill=col)
            d.line([(X(cx + hx * h + r - s * 0.08 - w / SS / 2), X(cy + h * 0.62)),
                    (X(cx + hx * h + r - s * 0.08 - w / SS / 2), X(cy - h * 0.75))], fill=col, width=w)
        d.line([(X(cx - 0.42 * h + r - s * 0.08 - w / SS / 2), X(cy - h * 0.75)),
                (X(cx + 0.42 * h + r - s * 0.08 - w / SS / 2), X(cy - h * 0.95))], fill=col, width=round(w * 1.4))
    elif name == "clock":  # clock: past notifications
        w = round(X(s * 0.11))
        d.ellipse([X(cx - h), X(cy - h), X(cx + h), X(cy + h)], outline=col, width=w)
        d.line([(X(cx), X(cy - h * 0.55)), (X(cx), X(cy)), (X(cx + h * 0.4), X(cy + h * 0.25))], fill=col, width=w,
               joint="curve")
    elif name == "gauge":  # gauge.with.needle: ring
        d.arc([X(cx - h), X(cy - h), X(cx + h), X(cy + h)], 150, 390, fill=col, width=round(X(s * 0.13)))
        d.line([(X(cx), X(cy)), (X(cx + h * 0.55), X(cy - h * 0.4))], fill=col, width=round(X(s * 0.12)))
    else:  # sparkle: ALICE's mark (four-pointed star)
        k = 0.22
        pts = [(0, -1), (k, -k), (1, 0), (k, k), (0, 1), (-k, k), (-1, 0), (-k, -k)]
        d.polygon([(X(cx + a * h), X(cy + b * h)) for a, b in pts], fill=col)
    img.alpha_composite(_down(big, img.size))


def ring(img: Image.Image, cx: float, cy: float, r: float, value: float, rgb: tuple, k: float) -> None:
    """Small progress ring (like the Clock app's remaining-time ring)."""
    big = _big(img.size)
    d = ImageDraw.Draw(big)
    w = round(2.6 * k * SS)
    box = [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS]
    d.ellipse(box, outline=(*rgb, 70), width=w)
    if value > 0:
        d.arc(box, -90, -90 + 360 * min(1.0, value), fill=(*rgb, 255), width=w)
    img.alpha_composite(_down(big, img.size))


# ------------------------------------------------------------------ content
@dataclass
class Entry:
    """One thing to show in the Island."""
    key: str
    kind: str              # status | card
    glyph: str
    title: str
    value: str = ""
    body: str = ""
    severity: str = "info"
    angle: float = 0.0
    progress: float | None = None
    seq: int = 0

    @property
    def rgb(self) -> tuple:
        return COLORS.get(self.severity, BLUE)


def _split(text: str) -> tuple[str, str]:
    t = tr_sentence(" ".join(str(text or "").split()))
    if " · " in t:
        a, b = t.split(" · ", 1)
        return a, b
    m = re.match(r"^(.*?)\s+(~?[\d.,]+\s*\S*)$", t)  # e.g. "Yakıt ~4 dk" (fuel ~4 min) -> ("Yakıt", "~4 dk")
    return (m.group(1), m.group(2)) if m and m.group(1) else (t, "")


def entries(items: list[Item]) -> list[Entry]:
    """HUD commands -> Island contents, in order of importance (critical first, then newer ones at the same level).
    The edge light and focus ring are drawn separately; the focus ring's text goes to the Island."""
    out: list[Entry] = []
    for it in items:
        d, key = it.data, it.id or f"#{it.seq}"
        sev = d.get("severity", "info")
        if it.kind == "arrow":
            title, value = _split(d.get("text") or "Tehdit")
            c = clock_of(d.get("clock"))
            out.append(Entry(key, "status", "arrow" if c else "radar", title, value, severity=sev,
                             angle=(c or 12) % 12 * 30.0 if c else 0.0, seq=it.seq))
        elif it.kind == "label":
            title, value = _split(d.get("text") or "")
            g = d.get("icon") or ("drop" if re.search(r"yak[ıi]t", title, re.I) else
                                  "warning" if sev != "info" else "sparkle")
            out.append(Entry(key, "status", g, title, value, severity=sev, seq=it.seq))
        elif it.kind == "progress":
            title, value = _split(d.get("text") or "")
            g = d.get("icon") or ("thermometer" if "°" in (d.get("text") or "") else "gauge")
            out.append(Entry(key, "status", g, title, value, severity=sev, seq=it.seq,
                             progress=max(0.0, min(1.0, float(d.get("value") or 0)))))
        elif it.kind in ("box", "cursor") and d.get("text"):
            out.append(Entry(key, "status", "cursor", tr_sentence(str(d["text"])), severity="info", seq=it.seq))
        elif it.kind == "card":
            title = str(d.get("title") or "ALICE")
            g = d.get("icon") or ("check" if title.lower().startswith("düşürdün") else
                                  "warning" if sev in ("warn", "critical") else "sparkle")
            out.append(Entry(key, "card", g, title, body=" ".join(str(d.get("text") or "").split()), severity=sev,
                             seq=it.seq))
    # critical threat first; then a newly arrived card (short-lived, expands the Island); then by importance
    out.sort(key=lambda e: (e.severity != "critical", e.kind != "card", PRIORITY.get(e.severity, 2), -e.seq))
    return out


# ------------------------------------------------------------------ Island
@dataclass
class Layout:
    w: float
    h: float
    r: float


def island_layout(e: Entry | None, k: float) -> Layout:
    """Island size the content asks for. Compact: 38 pt tall capsule; expanded (card): 400 pt wide, corner 26."""
    if e is None:
        return Layout(126 * k, 38 * k, 19 * k)  # idle: small capsule (not shown)
    if e.kind == "card":  # HIG: only as much room as the content needs; at most 400 pt
        fb = font(round(14 * k), 400, False)
        need = 72 * k + max(_tw(e.title, font(round(16 * k), 600)), _tw(e.body, fb) if e.body else 0)
        w = min(400 * k, max(240 * k, need))
        lines = _wrap(e.body, fb, round(w - 72 * k)) if e.body else []
        h = (20 + 22 + (6 + 19 * len(lines) if lines else 0) + 18) * k
        return Layout(w, max(h, 64 * k), 26 * k)
    ft = font(round(15 * k), 600)
    w = 14 * k + 20 * k + 9 * k + _tw(e.title, ft) + (10 * k + _tw(e.value, ft) if e.value else 0) + 16 * k
    if e.progress is not None:
        w += 26 * k
    return Layout(max(w, 112 * k), 38 * k, 19 * k)


MARGIN = 48  # shadow and glow allowance (in k units): so the blur isn't cut off


def _ss_mask(W: int, H: int, box: tuple, r: float) -> Image.Image:
    big = Image.new("L", (W * SS, H * SS), 0)
    ImageDraw.Draw(big).polygon([(x * SS, y * SS) for x, y in squircle(box, r)], fill=255)
    return big.resize((W, H), Image.LANCZOS)


@lru_cache(maxsize=64)
def _glass_parts(w: int, h: int, r: int, crit: bool | str, pulse_q: float, k: float) -> tuple:
    """Frosted glass parts (once per size): (shadow or glow, shape mask, hairline + light)."""
    m = round(MARGIN * k)
    W, H = w + 2 * m, h + 2 * m
    box = (m, m, m + w, m + h)
    q = 4  # blur at quarter resolution: looks the same, 16x cheaper
    under = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    small = Image.new("RGBA", (max(1, W // q), max(1, H // q)), (0, 0, 0, 0))
    listen = crit == "listen"
    crit = bool(crit) and not listen
    if crit:  # critical: breathing red glow
        g = (3 + 3 * pulse_q) * k
        poly = squircle((box[0] - g, box[1] - g, box[2] + g, box[3] + g), r + g)
        fill, blur = (*GLOW["critical"], int(80 + 70 * (1 - pulse_q))), 9 * k
    elif listen:  # listening: soft blue shimmer like Siri/Gemini, calmer than critical
        g = (2 + 3 * pulse_q) * k
        poly = squircle((box[0] - g, box[1] - g, box[2] + g, box[3] + g), r + g)
        fill, blur = (*BRAND_BLUE, int(55 + 45 * pulse_q)), 11 * k
    else:  # very light shadow like Liquid Glass: the glass stays visible even against an open sky
        poly = squircle((box[0], box[1] + 3 * k, box[2], box[3] + 3 * k), r)
        fill, blur = (0, 0, 0, 46), 10 * k
    ImageDraw.Draw(small).polygon([(x / q, y / q) for x, y in poly], fill=fill)
    under.alpha_composite(small.filter(ImageFilter.GaussianBlur(blur / q)).resize((W, H), Image.BILINEAR))
    mask = _ss_mask(W, H, box, r)
    big = _big((W, H))
    d = ImageDraw.Draw(big)
    pts = [(x * SS, y * SS) for x, y in squircle(box, r)]
    d.line(pts + [pts[0]], fill=(*RED, 220) if crit else HAIRLINE,
           width=max(1, round((1.6 if crit else 1.0) * k * SS)))
    over = big.resize((W, H), Image.LANCZOS)
    # Top-edge light: inner shape minus the inner shape shifted down by 1 px = a thin strip only on the top edge, fading
    # toward the sides. (Tracing the outline points in order left a "ear" at the corners; seen on the live screen.)
    inner = _ss_mask(W, H, (box[0] + k, box[1] + k, box[2] - k, box[3] - k), max(0.0, r - k))
    shifted = Image.new("L", (W, H), 0)
    shifted.paste(inner, (0, max(1, round(1.2 * k))))
    band = ImageChops.subtract(inner, shifted)
    fade = Image.linear_gradient("L").rotate(90).resize((W, H))  # left to right 0..255
    fade = fade.point(lambda v: int(255 * max(0.0, 1 - abs(v / 255 - 0.5) * 2) ** 0.6))
    band = ImageChops.multiply(band, fade).point(lambda v: v * HIGHLIGHT[3] // 255)
    shine = Image.new("RGBA", (W, H), HIGHLIGHT[:3] + (0,))
    shine.putalpha(band)
    over.alpha_composite(shine)
    return under, mask, over


def _glass(w: int, h: int, r: int, crit: bool | str, pulse_q: float, k: float, behind: Image.Image | None) -> Image.Image:
    """One glass surface: the (blurred) screen behind + light veil, clipped to the shape; hairline and light on top."""
    under, mask, over = _glass_parts(w, h, r, crit, pulse_q, k)
    img = under.copy()
    if behind is not None and behind.size == under.size:
        fill = behind.convert("RGBA")
        fill.alpha_composite(Image.new("RGBA", fill.size, TINT))
    else:
        fill = Image.new("RGBA", under.size, TINT_SOLID)
    fill.putalpha(ImageChops.multiply(fill.getchannel("A"), mask))
    img.alpha_composite(fill)
    img.alpha_composite(over)
    return img


@lru_cache(maxsize=64)
def _content(sig: tuple, w: int, h: int, k: float) -> Image.Image:
    """The Island's content (icon, text, value), drawn once at the target size."""
    kind, gname, title, value, body, sev, angle, progress = sig
    rgb = COLORS.get(sev, BLUE)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if kind == "card":
        pad = 20 * k
        glyph(img, gname, pad + 9 * k, 31 * k, 20 * k, LABEL if gname == "sparkle" else rgb)
        d = ImageDraw.Draw(img)
        tx = pad + 30 * k
        text(d, (tx, 31 * k), title, font(round(16 * k), 600), (*LABEL, 255), "lm")
        if body:
            fb = font(round(14 * k), 400, False)
            y = 48 * k
            for ln in _wrap(body, fb, round(w - tx - pad)):
                text(d, (tx, y), ln, fb, LABEL_2, "la", tracking=-0.01)
                y += 19 * k
        return img
    cy = h / 2
    glyph(img, gname, 24 * k, cy, 18 * k, rgb, float(angle))
    d = ImageDraw.Draw(img)
    ft = font(round(15 * k), 600)
    text(d, (43 * k, cy), title, ft, (*LABEL, 255), "lm")
    right = w - 16 * k
    if progress is not None:
        ring(img, right - 8 * k, cy, 8 * k, progress, rgb, k)
        right -= 26 * k
    if value:
        text(d, (right, cy), value, ft, (*rgb, 255), "rm")
    return img


def _sig(e: Entry) -> tuple:
    return (e.kind, e.glyph, e.title, e.value, e.body, e.severity, round(e.angle),
            None if e.progress is None else round(e.progress, 2))


def _fade(img: Image.Image, a: float) -> Image.Image:
    if a >= 0.999:
        return img
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda v: int(v * max(0.0, a))))
    return out


def _behind(backdrop: Image.Image | None, x: int, y: int, W: int, H: int) -> Image.Image | None:
    """The part of the blurred background that falls under this surface (canvas coordinates)."""
    if backdrop is None:
        return None
    return backdrop.crop((x, y, x + W, y + H))


def draw_island(img: Image.Image, e: Entry | None, box: tuple, r: float, k: float, content_alpha: float,
                pulse: float = 0.0, backdrop: Image.Image | None = None) -> None:
    x0, y0, x1, y1 = box
    w, h = max(1, round(x1 - x0)), max(1, round(y1 - y0))
    glow = "critical" if e is not None and e.severity == "critical" else "listen" if e is not None and         e.glyph == "wave" else ""
    m = round(MARGIN * k)
    px, py = round(x0) - m, round(y0) - m
    body = _glass(w, h, round(min(r, h / 2)), glow, round(pulse * 6) / 6 if glow else 0.0, k,
                  _behind(backdrop, px, py, w + 2 * m, h + 2 * m))
    img.alpha_composite(body, (px, py))
    if e is None or content_alpha <= 0.01:
        return
    lay = island_layout(e, k)
    c = _content(_sig(e), round(lay.w), round(lay.h), k)
    if c.width > w or c.height > h:  # spring hasn't opened yet: don't show the overflow
        c = c.crop((0, 0, min(c.width, w), min(c.height, h)))
    img.alpha_composite(_fade(c, content_alpha), (round(x0), round(y0)))


def draw_minimal(img: Image.Image, e: Entry, cx: float, cy: float, d_: float, k: float, alpha: float,
                 backdrop: Image.Image | None = None) -> None:
    """Separate small glass circle beside the Island: only the icon of the second/third content (HIG minimal view)."""
    d = max(2, round(d_))
    m = round(MARGIN * k)
    px, py = round(cx - d / 2) - m, round(cy - d / 2) - m
    layer = _glass(d, d, d // 2, e.severity == "critical", 0.0, k, _behind(backdrop, px, py, d + 2 * m, d + 2 * m))
    glyph(layer, e.glyph, m + d / 2, m + d / 2, 16 * k, COLORS.get(e.severity, BLUE), e.angle)
    img.alpha_composite(_fade(layer, alpha), (px, py))


class Spring:
    """Damped spring (overshoots slightly, then settles): the Island's size changes this way based on its content."""

    def __init__(self, value: float, stiffness: float = 260.0, damping: float = 24.0):
        self.x, self.v, self.target = value, 0.0, value
        self.k, self.c = stiffness, damping

    def step(self, dt: float) -> bool:
        """Advance one frame; True if it is still moving."""
        dt = min(dt, 1 / 30)
        for _ in range(4):
            h = dt / 4
            a = self.k * (self.target - self.x) - self.c * self.v
            self.v += a * h
            self.x += self.v * h
        if abs(self.target - self.x) < 0.3 and abs(self.v) < 3:
            self.x, self.v = self.target, 0.0
            return False
        return True


class Island:
    """Live state of the Island: which content, size springs, content transitions.
    `frame` gives the image for each frame."""

    MAX_W, MAX_H, MINIMALS = 420, 170, 2

    def __init__(self, sw: int, sh: int, top: float = 10.0, backdrop=None):
        self.backdrop = backdrop   # () -> blurred background the size of the Island's canvas, or None
        self.k = scale_for(sh)
        k = self.k
        self.sw, self.sh = sw, sh
        self.cw, self.ch = round((self.MAX_W + 2 * 60 + 2 * 46 + 2 * MARGIN) * k), round((self.MAX_H + 40 + MARGIN) * k)
        self.x, self.y = round(sw / 2 - self.cw / 2), 0
        self.top = top * k
        idle = island_layout(None, k)
        self.w, self.h, self.r = Spring(idle.w), Spring(idle.h), Spring(idle.r)
        self.shown = 0.0           # 0-1: is the Island itself visible (fades out when idle)
        self.current: Entry | None = None
        self.prev: Entry | None = None
        self.changed_at = -1e9
        self.minimals: list[Entry] = []
        self.started = -1e9
        self.last_t: float | None = None
        self.angle: float | None = None     # arrow angle being drawn now (turns to the new direction, doesn't jump)
        self.turn = (0.0, 0.0, -1e9)        # (from, to, moment it started)
        self.calm = False                   # only the pulse is running (no size/transition/turn): can be drawn sparsely

    def update(self, ents: list[Entry], now: float) -> bool:
        """New content list. True if there is anything to show."""
        top = ents[0] if ents else None
        cur = self.current
        if top is not None and (cur is None or top.key != cur.key or (top.title, top.value, top.body, top.angle,
                                                                        top.progress) !=
                                (cur.title, cur.value, cur.body, cur.angle, cur.progress)):
            same = cur is not None and top.key == cur.key
            if not same:
                self.prev, self.changed_at = cur, now
                self.angle = top.angle
            elif top.angle != cur.angle:
                self.turn = (self.angle if self.angle is not None else cur.angle, top.angle, now)
            if cur is None:
                self.started = now
            self.current = top
        elif top is None and cur is not None:
            self.prev, self.current, self.changed_at = cur, None, now
        self.minimals = ents[1:1 + self.MINIMALS]
        lay = island_layout(self.current or self.prev, self.k)
        self.w.target, self.h.target, self.r.target = lay.w, lay.h, lay.r
        return self.current is not None or self.shown > 0.01

    def frame(self, now: float) -> tuple[Image.Image, bool]:
        """(image, still moving or not)."""
        dt = 1 / 60 if self.last_t is None else now - self.last_t
        self.last_t = now
        moving = any([self.w.step(dt), self.h.step(dt), self.r.step(dt)])
        target_shown = 1.0 if self.current is not None else 0.0
        if abs(self.shown - target_shown) > 0.001:
            self.shown += (target_shown - self.shown) * min(1.0, dt / (FADE_IN if target_shown else FADE_OUT) * 2.2)
            if abs(self.shown - target_shown) < 0.01:
                self.shown = target_shown
            moving = True
        t = (now - self.changed_at) / 0.32  # content transition: old fades out, new fades in
        content = self.current if t >= 0.4 or self.prev is None else self.prev
        content_alpha = (min(1.0, (t - 0.4) / 0.6) if t < 1 else 1.0) if content is self.current else max(0.0, 1 - t / 0.4)
        if t < 1:
            moving = True
        a0, a1, at = self.turn
        tt = (now - at) / TURN
        if self.current is not None and tt < 1.2:
            diff = ((a1 - a0) + 180) % 360 - 180
            self.angle = (a0 + diff * ease_out(tt)) % 360
            content = replace(content, angle=self.angle) if content is self.current else content
            moving = True
        pulse = 0.0
        self.calm = False
        if self.current is not None and self.current.severity == "critical":
            pulse = 0.5 + 0.5 * math.sin((now - self.started) * 2 * math.pi / PULSE)
            self.calm = not moving
            moving = True
        elif self.current is not None and self.current.glyph == "wave":  # waves move while listening; glass glows
            pulse = 0.5 + 0.5 * math.sin((now - self.started) * 2 * math.pi / LISTEN_PULSE)
            if content is self.current:
                content = replace(content, angle=round(now * WAVE_SPEED) % 8 * 45.0)  # 8 phases: the cache holds them
            self.calm = not moving
            moving = True
        img = Image.new("RGBA", (self.cw, self.ch), (0, 0, 0, 0))
        k = self.k
        bd = None
        if self.shown > 0.005 and self.backdrop is not None:
            try:
                bd = self.backdrop()
            except Exception:  # noqa: BLE001 - if the background can't be captured, use plain clear glass
                bd = None
        if self.shown > 0.005:
            cx, top = self.cw / 2, self.top
            w, h = self.w.x, self.h.x
            grow = 0.86 + 0.14 * self.shown  # starts small while appearing
            w, h = w * grow, h * grow
            box = (cx - w / 2, top, cx + w / 2, top + h)
            draw_island(img, content, box, min(self.r.x, h / 2), k, content_alpha, pulse, bd)
            x = box[2] + 6 * k
            for e in self.minimals:  # small separate circles on the right
                d_ = 38 * k * grow
                draw_minimal(img, e, x + d_ / 2, top + d_ / 2, d_, k, 1.0, bd)
                x += d_ + 6 * k
            if self.shown < 0.999:
                img.putalpha(img.getchannel("A").point(lambda v: int(v * self.shown)))
        return img, moving


# ------------------------------------------------------------------ edge light and focus ring
@dataclass
class Sprite:
    key: str
    item: Item
    x: int
    y: int
    w: int
    h: int
    angle: float = 0.0


def layout(items: list[Item], sw: int, sh: int) -> list[Sprite]:
    """Drawn outside the Island: edge light for a critical/warning threat, and the focus ring
    around the place to click."""
    k = scale_for(sh)
    out: list[Sprite] = []
    for it in items:
        key = it.id or f"#{it.seq}"
        if it.kind == "edge":
            out.append(_edge_sprite(key, it, sw, sh, k))
        elif it.kind == "box":
            d = it.data
            pad = round(16 * k)
            out.append(Sprite(key, it, round(float(d["x"])) - pad, round(float(d["y"])) - pad,
                              round(float(d["w"])) + 2 * pad, round(float(d["h"])) + 2 * pad))
    return out


def _edge_sprite(key: str, it: Item, sw: int, sh: int, k: float) -> Sprite:
    """Soft light on the edge the beam hits; a round light in the corner if it is near a corner."""
    ang = (clock_of(it.data.get("clock")) or 12) % 12 * 30.0
    dx, dy = math.sin(math.radians(ang)), -math.cos(math.radians(ang))
    tx = (sw / 2) / abs(dx) if abs(dx) > 1e-6 else math.inf
    ty = (sh / 2) / abs(dy) if abs(dy) > 1e-6 else math.inf
    t = min(tx, ty)
    ex, ey = sw / 2 + dx * t, sh / 2 + dy * t
    depth = round(180 * k)  # deep: so the brightest part isn't hidden and lost under the taskbar
    if min(ex, sw - ex) < sw * 0.14 and min(ey, sh - ey) < sh * 0.14:
        r = round(320 * k)
        return Sprite(key, it, 0 if ex < sw / 2 else sw - r, 0 if ey < sh / 2 else sh - r, r, r, ang)
    if tx <= ty:
        length = round(sh * 0.5)
        return Sprite(key, it, 0 if ex < sw / 2 else sw - depth, round(min(max(0, ey - length / 2), sh - length)),
                      depth, length, ang)
    length = round(sw * 0.34)
    return Sprite(key, it, round(min(max(0, ex - length / 2), sw - length)), 0 if ey < sh / 2 else sh - depth,
                  length, depth, ang)


@lru_cache(maxsize=16)
def _edge_mask(w: int, h: int, left: bool, top: bool) -> Image.Image:
    sx, sy = max(2, w // 4), max(2, h // 4)
    mask = Image.new("L", (sx, sy), 0)
    px = mask.load()
    square = abs(w - h) < 4
    for j in range(sy):
        for i in range(sx):
            u, v = (i + 0.5) / sx, (j + 0.5) / sy
            if square:
                val = max(0.0, 1 - math.hypot(u if left else 1 - u, v if top else 1 - v)) ** 2.4
            elif w < h:
                val = max(0.0, 1 - (u if left else 1 - u)) ** 1.5 * math.exp(-((v - 0.5) ** 2) / 0.05)
            else:
                val = max(0.0, 1 - (v if top else 1 - v)) ** 1.5 * math.exp(-((u - 0.5) ** 2) / 0.05)
            px[i, j] = int(255 * val)
    return mask.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))


def render(sp: Sprite, sh: int, alpha: float = 1.0, grow: float = 1.0, pulse: float = 0.0) -> Image.Image:
    k = scale_for(sh)
    img = Image.new("RGBA", (max(1, sp.w), max(1, sp.h)), (0, 0, 0, 0))
    sev = sp.item.data.get("severity", "info")
    rgb = GLOW.get(sev, GLOW["info"]) if sp.item.kind == "edge" else BLUE  # edge light is vivid; the ring is brand blue
    if sp.item.kind == "edge":
        peak = (0.7 + 0.25 * (1 - pulse)) if sev == "critical" else 0.6
        mask = _edge_mask(img.width, img.height, sp.x <= 0, sp.y <= 0).point(lambda v: int(v * peak))
        glow = Image.new("RGBA", img.size, (*rgb, 0))
        glow.putalpha(mask)
        img.alpha_composite(glow)
    elif sp.item.kind == "box":  # macOS focus ring: blue, concentric continuous corner, soft outer glow
        d = sp.item.data
        pad = 16 * k
        bw, bh = float(d["w"]), float(d["h"])
        r = min(10 * k, bh / 3, bw / 3)
        g = 3 * k
        box = (pad - g, pad - g, pad + bw + g, pad + bh + g)
        halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(halo).line(squircle(box, r + g) + [squircle(box, r + g)[0]], fill=(*BLUE, 150),
                                  width=round(6 * k))
        img.alpha_composite(halo.filter(ImageFilter.GaussianBlur(5 * k)))
        _shape(img, box, r + g, (0, 0, 0, 0), (*BLUE, 235), 3 * k)
    return adjust(img, alpha, grow)


def _with_alpha(img: Image.Image, a: float) -> Image.Image:
    if a >= 0.999:
        return img
    r, g, b, al = img.split()
    return Image.merge("RGBA", (r, g, b, al.point(lambda v: int(v * max(0.0, a)))))


def adjust(img: Image.Image, alpha: float = 1.0, grow: float = 1.0) -> Image.Image:
    if abs(grow - 1.0) > 1e-3:
        W, H = img.size
        w, h = max(1, round(W * grow)), max(1, round(H * grow))
        scaled = img.resize((w, h), Image.BILINEAR)
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if grow <= 1:
            out.alpha_composite(scaled, ((W - w) // 2, (H - h) // 2))
        else:
            ox, oy = (w - W) // 2, (h - H) // 2
            out.alpha_composite(scaled.crop((ox, oy, ox + W, oy + H)))
        img = out
    return _with_alpha(img, alpha)


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def spring(t: float) -> float:
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    return 1 - math.exp(-6.5 * t) * math.cos(7.5 * t) * (1 - t) ** 0.5


@dataclass
class Anim:
    """Animation of the edge light and focus ring."""
    shown_at: float
    gone_at: float | None = None
    angle: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def frame(self, now: float, sp: Sprite) -> tuple[float, float, float, bool]:
        t_in = (now - self.shown_at) / FADE_IN
        alpha = ease_out(t_in)
        moving = t_in < 1.5
        if self.gone_at is not None:
            t_out = (now - self.gone_at) / FADE_OUT
            alpha *= 1 - ease_out(t_out)
            moving = t_out < 1
        grow = 1.08 - 0.08 * spring(t_in / 1.5) if sp.item.kind == "box" else 1.0  # the focus ring settles in from outside
        pulse = 0.0
        if sp.item.kind == "edge" and sp.item.data.get("severity") == "critical":
            pulse = 0.5 + 0.5 * math.sin((now - self.shown_at) * 2 * math.pi / PULSE)
            if self.gone_at is None:  # while fading out the pulse isn't "motion", or the light would never turn off
                moving = True
        return alpha, grow, pulse, moving


# ------------------------------------------------------------------ ALICE's pointer
CLICKS = ("click", "double_click", "right_click", "middle_click")
GLIDE_MIN, GLIDE_MAX = 0.16, 0.34  # sec: glide time grows with distance; it settles before the 0.4 s click preview
RIPPLE = 0.55                      # sec: ring that spreads and fades at the clicked spot
POINTER_IDLE = 1.5                 # sec: stays still this long after the last move, then fades


class Pointer:
    """ALICE's own pointer (separate from the real cursor, visual only): it doesn't teleport to a new target;
    it glides along a curved path. A ring wave spreads on click; it fades when idle. The window moves with the
    pointer tip, so the image stays small."""

    def __init__(self, sh: int):
        self.k = scale_for(sh)
        self.size = round(120 * self.k)
        self.seq = -1
        self.start = self.end = self.ctrl = (0.0, 0.0)
        self.t0, self.dur = -1e9, 0.0
        self.click = False
        self.shown_at: float | None = None
        self.gone_at: float | None = None

    @property
    def active(self) -> bool:
        return self.shown_at is not None

    def go(self, x: float, y: float, action: str, now: float, origin: tuple[float, float] | None = None) -> None:
        """New target. If visible, starts from where it is; otherwise from the real cursor's position
        (if that is unknown, it appears at the target)."""
        if self.active and self.gone_at is None:
            start = self.at(now)
        else:
            start = origin if origin is not None else (x, y)
            self.shown_at = now
        self.gone_at = None
        dx, dy = x - start[0], y - start[1]
        dist = math.hypot(dx, dy)
        bend = min(0.18 * dist, 140 * self.k)  # a slight curve, like a hand-made movement
        mx, my = (start[0] + x) / 2, (start[1] + y) / 2
        nx, ny = (-dy / dist, dx / dist) if dist > 1 else (0.0, 0.0)
        self.start, self.end, self.ctrl = start, (x, y), (mx + nx * bend, my + ny * bend)
        self.t0, self.dur = now, 0.0 if dist < 2 else min(GLIDE_MAX, GLIDE_MIN + dist / (5000 * self.k))
        self.click = action in CLICKS

    def leave(self, now: float) -> None:
        if self.active and self.gone_at is None:
            self.gone_at = now

    def _t(self, now: float) -> float:
        return 1.0 if self.dur <= 0 else min(1.0, max(0.0, (now - self.t0) / self.dur))

    def at(self, now: float) -> tuple[float, float]:
        u = self._t(now)
        u = u * u * (3 - 2 * u)  # soft start and stop
        (x0, y0), (cx, cy), (x1, y1) = self.start, self.ctrl, self.end
        a, b, c = (1 - u) ** 2, 2 * (1 - u) * u, u * u
        return a * x0 + b * cx + c * x1, a * y0 + b * cy + c * y1

    def frame(self, now: float) -> tuple[Image.Image | None, float, float, bool]:
        """(image or None: fully faded out, top-left x, y, whether it is moving)."""
        if self.shown_at is None:
            return None, 0.0, 0.0, False
        k, S = self.k, self.size
        x, y = self.at(now)
        alpha = ease_out((now - self.shown_at) / 0.18)
        if self.gone_at is not None:
            t_out = (now - self.gone_at) / FADE_OUT
            if t_out >= 1:
                self.shown_at = self.gone_at = None
                return None, 0.0, 0.0, False
            alpha *= 1 - ease_out(t_out)
        landed = self.t0 + self.dur
        tr = (now - landed) / RIPPLE
        moving = self._t(now) < 1 or (self.click and tr < 1) or self.gone_at is not None or alpha < 0.999
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        c = S / 2
        if self.click and 0 <= tr < 1:  # clicked: ring spreads and fades
            big = _big(img.size)
            d = ImageDraw.Draw(big)
            rr = (7 + 34 * ease_out(tr)) * k
            d.ellipse([(c - rr) * SS, (c - rr) * SS, (c + rr) * SS, (c + rr) * SS],
                      outline=(*BRAND_BLUE, int(210 * (1 - tr))), width=round(2.6 * k * SS))
            img.alpha_composite(_down(big, img.size))
        _pointer_glyph(img, c, c, 26 * k, k)
        return _with_alpha(img, alpha), x - c, y - c, moving


@lru_cache(maxsize=4)
def _pointer_layer(size: int, s: float, k: float) -> Image.Image:
    """Blue arrow whose tip sits at (cx, cy): white edge, slight shadow (so it stands out on any background)."""
    pts = [(0, 0), (0, 0.78), (0.21, 0.6), (0.36, 0.95), (0.5, 0.89), (0.35, 0.55), (0.62, 0.55)]
    c = size / 2
    poly = [(c + a * s, c + b * s) for a, b in pts]
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon([(x + 1.5 * k, y + 2.5 * k) for x, y in poly], fill=(0, 0, 0, 90))
    out = shadow.filter(ImageFilter.GaussianBlur(3 * k))
    big = _big((size, size))
    d = ImageDraw.Draw(big)
    d.polygon([(x * SS, y * SS) for x, y in poly], fill=(*BRAND_BLUE, 255), outline=(255, 255, 255, 255),
              width=round(2.2 * k * SS))
    out.alpha_composite(_down(big, (size, size)))
    return out


def _pointer_glyph(img: Image.Image, cx: float, cy: float, s: float, k: float) -> None:
    layer = _pointer_layer(img.width, s, k)
    img.alpha_composite(layer, (round(cx - img.width / 2), round(cy - img.height / 2)))


def blur_backdrop(img: Image.Image, k: float = 1.0) -> Image.Image:
    """Behind the glass: the screen piece is blurred at quarter resolution and slightly saturated (Apple's glass
    material softens color rather than washing it out). Cheap: about 2 ms for a 700x200 piece."""
    from PIL import ImageEnhance

    q = 4
    small = img.convert("RGB").resize((max(1, img.width // q), max(1, img.height // q)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(18 * k / q))
    small = ImageEnhance.Color(small).enhance(1.35)
    return small.resize(img.size, Image.BILINEAR)


def preview(items: list[dict[str, Any]], sw: int = 1920, sh: int = 1080,
            background: Image.Image | None = None) -> Image.Image:
    """Show the final state of the commands in one image (for reports and tests; doesn't open a window)."""
    from alice.hud.model import HudModel

    m = HudModel()
    for msg in items:
        m.apply(msg, 0.0)
    img = (background.convert("RGBA").resize((sw, sh)) if background is not None
           else Image.new("RGBA", (sw, sh), (0, 0, 0, 0)))
    for sp in layout(m.visible(), sw, sh):
        img.alpha_composite(render(sp, sh, pulse=0.3), (sp.x, sp.y))
    isl = Island(sw, sh)
    if background is not None:  # frosted glass: blurred version of the image behind
        bg = img.crop((isl.x, isl.y, isl.x + isl.cw, isl.y + isl.ch)).convert("RGB")
        blurred = blur_backdrop(bg, isl.k)
        isl.backdrop = lambda: blurred
    isl.update(entries(m.visible()), 0.0)
    for t in range(0, 120):  # let the springs and transitions settle
        frame, _ = isl.frame(t / 60)
    img.alpha_composite(frame, (isl.x, isl.y))
    return img
