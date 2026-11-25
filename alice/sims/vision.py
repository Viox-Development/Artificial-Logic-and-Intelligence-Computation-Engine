"""Screen-based missile vision: War Thunder does not expose missile or RWR data in its API, but shows it on screen.

Only in flight, the small screen region containing just the RWR indicator is captured 10 times per second in the process
(mss). Recognition is rule-based: a color threshold for red/orange markers, and clock direction from the angle
around the circle's center. A blinking warning is verified over a few frames, so a single-frame false alarm does
not count. The result is added to the contact list as `missile: True`, so the existing "Füze! Saat 4" announcement
and the HUD arrow work without any changes.

It does not touch the game or read its memory: it takes screenshots the way OBS does. The GPU is not used.

  alice vision region X Y G Y    set the RWR region (physical pixels), save the marked image
  alice vision show              frame the configured region on a full-screen screenshot and save it (for confirmation)
  alice vision record            collect samples with a hotkey: Ctrl+Alt+M missile present, Ctrl+Alt+N none
  alice vision test DOSYA...     try recognition on saved images
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home
from alice.i18n import _

DEFAULTS: dict[str, Any] = {
    "method": "text",     # text: War Thunder's turquoise RWR text (LAUNCH/TRACK/MSL); red: the old red marker
    "fps": 6.0,           # LAUNCH blinks ~2 times per second: at 2 frames/sec every frame could land on the off phase
    "red_min": 170,       # minimum red channel value
    "red_margin": 70,     # red must exceed green and blue by at least this much (includes orange, excludes white/gray)
    "min_pixels": 12,     # fewer marked pixels than this count as noise
    "center_ignore": 0.18,  # the circle's center (own aircraft icon) is ignored out to this fraction of the radius
    "confirm": 2,         # must show in this many of the last 'window' frames (blinking warning: half is enough)
    "window": 5,
    "hold": 1.5,          # a confirmed warning ends if not seen for this long (sec)
}
IDLE_AFTER = 3.0  # capture stops if no flight data arrives for this long (hangar, death)


def samples_dir() -> Path:
    return alice_home() / "vision"


@dataclass
class Detection:
    clock: int
    pixels: int


def detect(rgb, cfg: dict[str, Any] | None = None) -> Detection | None:
    """One frame (height x width x 3, RGB) -> missile/RWR marker and clock direction. None if there is none."""
    import numpy as np

    c = {**DEFAULTS, **(cfg or {})}
    a = np.asarray(rgb, dtype=np.int16)
    if a.ndim != 3 or a.shape[2] < 3 or a.shape[0] < 4 or a.shape[1] < 4:
        return None
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    m = int(c["red_margin"])
    mask = (r >= int(c["red_min"])) & (r - g >= m) & (r - b >= m)
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    cx, cy = (w - 1) / 2, (h - 1) / 2
    dx, dy = xs - cx, ys - cy
    radius = min(w, h) / 2
    keep = np.hypot(dx, dy) >= radius * float(c["center_ignore"])
    dx, dy = dx[keep], dy[keep]
    if len(dx) < int(c["min_pixels"]):
        return None
    ang = math.degrees(math.atan2(float(dx.mean()), float(-dy.mean()))) % 360  # 0 = up, clockwise
    clock = round(ang / 30) % 12 or 12
    return Detection(clock, int(len(dx)))


# War Thunder's RWR (from Medal clips, 2026-11-03): turquoise, not red. First "TRACK" appears above the circle (you are
# locked on); when a missile is fired a "LAUNCH" box appears, and an "MSL" text appears inside the circle
# in the missile's direction. At 1080 px height, on the left edge of the screen: circle center (187, 539),
# radius ~113; the region includes
# the label strip. The HUD scales with height, so at other resolutions everything is multiplied by height/1080.
WT_REGION_1080 = (60, 380, 255, 285)       # x, y, width, height
WT_CENTER = (0.498, 0.615)                 # circle center (own aircraft icon), as a fraction of the region
OCR_UPSCALE = 2                            # small HUD text reads better when upscaled


_TR = str.maketrans("ŞşĞğÇçÜüİıÖö$5", "SsGgCcUuIiOoSS")
MSL_RX = __import__("re").compile(r"MS[LIU1]|M[A-Z]L\b|[A-Z]SL")  # the reading engine can output "MSÜ" or "MğL" for MSL


def _plain(text: str) -> str:
    """Simplify the letters the Turkish reading engine produces: "MSÜ" -> "MSU", "LAUNŞH" -> "LAUNSH"."""
    return str(text).translate(_TR).upper()


def _like(text: str, word: str) -> bool:
    """Does this text resemble the word closely enough? (The reading engine may confuse one letter.)"""
    import difflib

    return word in text or any(difflib.SequenceMatcher(None, text[i:i + len(word)], word).ratio() >= 0.75
                               for i in range(max(1, len(text) - len(word) + 1)))


def hud_mask(img):
    """Keep only War Thunder's turquoise HUD color (black text on white): the reading engine reads text over trees
    and sky much better this way ("LAUNCH" was not read at all in the raw image, but was read with the mask)."""
    import numpy as np
    from PIL import Image

    a = np.asarray(img.convert("RGB")).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    m = (g > 150) & (b > 120) & (r < 140) & (g - r > 70)
    return Image.fromarray(np.where(m, 0, 255).astype(np.uint8))


def default_region(screen_h: int) -> tuple[int, int, int, int]:
    f = screen_h / 1080
    return tuple(round(v * f) for v in WT_REGION_1080)  # type: ignore[return-value]


@dataclass
class TextDetection:
    clock: int | None   # None if MSL was not seen: a directionless warning (HUD shows the radar icon)
    launch: bool        # missile launched ("LAUNCH" or "MSL")
    track: bool         # lock-on ("TRACK")
    pixels: int = 0


def read_rwr(rgb, cfg: dict[str, Any] | None = None, words=None) -> TextDetection | None:
    """Missile/lock and direction from the RWR region text. In tests, words (word, (x, y, w, h)) replace OCR."""
    from PIL import Image

    import numpy as np

    c = {**DEFAULTS, **(cfg or {})}
    img = Image.fromarray(np.asarray(rgb).astype("uint8")) if not isinstance(rgb, Image.Image) else rgb
    k = OCR_UPSCALE
    if words is None:
        from alice.reflex.perceive import ocr_words

        big = hud_mask(img).resize((img.width * k, img.height * k), Image.LANCZOS)
        words = [(t, (x / k, y / k, w / k, h / k)) for t, (x, y, w, h) in ocr_words(big)]
    up = [(_plain(t), box) for t, box in words]
    launch = any(_like(t, "LAUNCH") for t, _box in up)
    track = any(_like(t, "TRACK") for t, _box in up)
    seen_msl = any(MSL_RX.search(t) for t, _box in up)
    # Direction comes only from a short "MSL" read on its own: the engine sometimes merges it with a nearby marker
    # ("M29MSL"); the middle of a merged word does not show the missile's position (clips jumped 12 -> 6 -> 1).
    msl = [box for t, box in up if MSL_RX.search(t) and len(t.strip(".,:;()'\"")) <= 4]
    if not (launch or track or seen_msl):
        return None
    clock = None
    if msl:
        cx, cy = (float(v) for v in c.get("rwr_center", WT_CENTER))
        x, y, w, h = msl[0]
        dx = (x + w / 2) - cx * img.width
        dy = (y + h / 2) - cy * img.height
        if (dx * dx + dy * dy) ** 0.5 >= 4:  # dead center: direction is unclear
            ang = math.degrees(math.atan2(dx, -dy)) % 360  # 0 = up (nose), clockwise
            clock = round(ang / 30) % 12 or 12
    return TextDetection(clock, launch or seen_msl, track)


class Confirmer:
    """Confirms a blinking warning: the marker must appear in at least 'confirm' of the last 'window' frames."""

    def __init__(self, confirm: int = 3, window: int = 5, hold: float = 1.5):
        self.confirm, self.hold = confirm, hold
        self.frames: deque[Detection | None] = deque(maxlen=window)
        self.last: Detection | None = None
        self.seen_at = -1e9

    def feed(self, d: Detection | None, now: float) -> Detection | None:
        self.frames.append(d)
        hits = [x for x in self.frames if x is not None]
        if len(hits) >= self.confirm:
            self.last, self.seen_at = hits[-1], now
        if self.last is not None and now - self.seen_at > self.hold:
            self.last = None
        return self.last


_local = threading.local()


def grab_region(region: tuple[int, int, int, int]):
    """Only this small region of the screen (x, y, width, height) -> RGB array. The mss object is opened once per
    thread: reopening it on every frame was eating 12% of a core."""
    from alice.tools.imaging import screen_capture
    import numpy as np

    sct = getattr(_local, "sct", None)
    if sct is None:
        from alice.tools.computer import _dpi_aware

        _dpi_aware()
        sct = _local.sct = screen_capture()
    x, y, w, h = region
    shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return np.frombuffer(shot.bgra, dtype=np.uint8).reshape(h, w, 4)[..., 2::-1]  # BGRA -> RGB


class MissileWatcher:
    """Watches the RWR region while flying; contacts() reports a confirmed missile warning as a contact."""

    def __init__(self, region: tuple[int, int, int, int], cfg: dict[str, Any] | None = None,
                 grab: Callable[[tuple[int, int, int, int]], Any] = grab_region):
        self.region = region
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.grab = grab
        self.confirmer = Confirmer(int(self.cfg["confirm"]), int(self.cfg["window"]), float(self.cfg["hold"]))
        self.locker = Confirmer(int(self.cfg["confirm"]), int(self.cfg["window"]), float(self.cfg["hold"]))
        self._lock_cur: Any = None
        self._poked = -1e9
        self._current: Detection | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors = 0

    def poke(self) -> None:
        """Flight data arrived: capture keeps running (or starts)."""
        self._poked = time.monotonic()
        self._wake.set()
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="alice-missile-vision")
            self._thread.start()

    def step(self, now: float | None = None) -> Detection | None:
        now = time.monotonic() if now is None else now
        lock = None
        try:
            frame = self.grab(self.region)
            if self.cfg.get("method", "text") == "text":
                t = read_rwr(frame, self.cfg)
                d = t if t is not None and t.launch else None
                lock = t if t is not None and t.track else None
            else:
                d = detect(frame, self.cfg)
        except Exception:  # noqa: BLE001 - a capture error (locked screen, fullscreen game) must not break flight
            self.errors += 1
            d = None
        cur = self.confirmer.feed(d, now)
        lk = self.locker.feed(lock, now)
        with self._lock:
            self._current, self._lock_cur = cur, lk
        return cur

    def _run(self) -> None:
        every = 1.0 / max(1.0, float(self.cfg["fps"]))
        while not self._stop.is_set():
            if time.monotonic() - self._poked > IDLE_AFTER:  # hangar, death: the screen is not captured
                with self._lock:
                    self._current = None
                self._wake.clear()
                self._wake.wait(5)
                continue
            t0 = time.monotonic()
            self.step(t0)
            self._stop.wait(max(0.0, every - (time.monotonic() - t0)))

    def contacts(self) -> list[dict[str, Any]]:
        with self._lock:
            d, lk = self._current, self._lock_cur
        if d is not None:  # missile (clock None if direction is unknown: announced without it, HUD shows radar icon)
            return [{"id": "vision", "name": "", "kind": "radar", "clock": d.clock, "lock": True, "missile": True,
                     "dist_m": None, "vision": True}]
        if lk is not None:  # locked on, but no missile
            return [{"id": "vision", "name": "", "kind": "radar", "clock": lk.clock, "lock": True, "missile": False,
                     "dist_m": None, "vision": True}]
        return []

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


def region_of(cfg: dict[str, Any]) -> tuple[int, int, int, int] | None:
    r = cfg.get("vision_region")
    if isinstance(r, (list, tuple)) and len(r) == 4:
        try:
            x, y, w, h = (int(v) for v in r)
        except (TypeError, ValueError):
            return None
        if w >= 8 and h >= 8:
            return x, y, w, h
    return None


# ---------------------------------------------------------------------- command
def _full_screenshot():
    from alice.tools.imaging import screen_capture
    import numpy as np

    from alice.tools.computer import _dpi_aware

    _dpi_aware()
    with screen_capture() as sct:
        shot = sct.grab(sct.monitors[0])
        left, top = sct.monitors[0]["left"], sct.monitors[0]["top"]
    return np.asarray(shot)[..., [2, 1, 0]], (left, top)


def save_marked(region: tuple[int, int, int, int], out: Path) -> Path:
    """Mark the region with a green frame on a full-screen screenshot (visual check that the region is right)."""
    from PIL import Image, ImageDraw

    rgb, (left, top) = _full_screenshot()
    img = Image.fromarray(rgb.astype("uint8"))
    x, y, w, h = region
    ImageDraw.Draw(img).rectangle([x - left, y - top, x - left + w, y - top + h], outline=(0, 255, 0), width=4)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def test_files(paths: list[Path], region: tuple[int, int, int, int] | None, cfg: dict[str, Any]) -> list[str]:
    """Recognition on saved images: if it is a full-screen screenshot, the configured region is cropped."""
    import numpy as np
    from PIL import Image

    out = []
    for p in paths:
        a = np.asarray(Image.open(p).convert("RGB"))
        if region is not None and a.shape[0] > region[3] and a.shape[1] > region[2]:
            x, y, w, h = region
            a = a[y:y + h, x:x + w]
        d = detect(a, cfg)
        out.append(f"{p.name}: " + (_("mark found, {clock} o'clock ({n} pixels)", clock=d.clock, n=d.pixels) if d else _("no mark")))
    return out


def record(cfg: dict[str, Any], seconds: float = 3600.0) -> int:
    """Collect samples with hotkeys. The label goes in the file name: missile-*.png / clear-*.png."""
    from PIL import Image

    from alice.hotkeys import WinHotkeys

    folder = samples_dir()
    folder.mkdir(parents=True, exist_ok=True)

    def save(label: str) -> None:
        rgb, _size = _full_screenshot()
        p = folder / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        Image.fromarray(rgb.astype("uint8")).save(p)
        print(_("saved: {path}", path=p))

    keys = WinHotkeys({"<ctrl>+<alt>+m": lambda: save("missile"), "<ctrl>+<alt>+n": lambda: save("clear")})
    if not keys.start():
        print(_("could not take the shortcuts (another program may hold them)"))
        return 1
    print(_("Collecting samples ({folder}). Ctrl+Alt+M while a missile warning is on screen, Ctrl+Alt+N when there is "
            "none. Ctrl+C when done.", folder=folder))
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass  # Ctrl+C ends sample collection early
    finally:
        keys.stop()
    return 0


def vision_cmd(argv: list[str]) -> int:
    from alice.config import load_config, set_config_value

    sims = load_config().data.get("sims", {}) or {}
    sub = argv[0] if argv else ""
    region = region_of(sims)
    cfg = {**DEFAULTS, **(sims.get("vision") or {})}
    if sub == "region" and len(argv) == 5:
        try:
            r = [int(v) for v in argv[1:5]]
        except ValueError:
            print(_("usage: alice vision region X Y WIDTH HEIGHT"))
            return 2
        set_config_value("sims", "vision_region", r)
        p = save_marked(tuple(r), samples_dir() / "bolge.png")  # type: ignore[arg-type]
        print(_("region saved: {region}. Check it is the right place: {path}", region=r, path=p))
        return 0
    if sub == "show":
        if region is None:
            print(_("no region set: alice vision region X Y WIDTH HEIGHT"))
            return 2
        print(_("marked image: {path}", path=save_marked(region, samples_dir() / "bolge.png")))
        return 0
    if sub == "record":
        return record(cfg)
    if sub == "test" and len(argv) > 1:
        files = [Path(a) for a in argv[1:]]
        if len(files) == 1 and files[0].is_dir():
            files = sorted(files[0].glob("*.png"))
        for line in test_files(files, region, cfg):
            print(line)
        return 0
    print(__doc__)
    return 2
