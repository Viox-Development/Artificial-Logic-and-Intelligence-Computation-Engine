"""Ekranı Jev'in seçebileceği numaralı öğe listesine çevir: erişilebilirlik ağacı + OCR, konum ve bağlamla.

Jev ekranı kendisi yorumlamakta (görsel, Box) henüz zayıf; en iyi çalışan Jev bilgisayar-kullanım projeleri
(typesafe-computer-use, otto, CUA-JEV) ekranı deterministik okuyup her öğeyi tek satırla anlatıyor:
`button 'Kaydet' (top-right)`, `text 'Uydurma bir savunma' (left-top; in the row of '14 Eki')`. Burada:

- erişilebilirlik ağacından (UIA / DOM) gelen kontroller ve ekrandaki yazılar (OCR) birleştirilir; ikisi aynı şeyse
  tek öğe olur (`ax+ocr`), iki seçenek oyu bölmesin;
- konum, pencerenin üçte birlik bölgeleriyle yazılır (sayı değil: Jev sayıları karşılaştıramaz);
- aynı adı taşıyan öğelere satır bağlamı eklenir (hangisi olduğu ancak satırdan anlaşılır);
- hedefi tekrarlayan yazılar atılır: kullanıcının "projelerdeki ilk projeye tıkla" mesajı ekranda göründüğü için
  Jev o yazıya tıklıyordu;
- liste hedefle ilgili olanlar önde olacak şekilde sınırlanır.

OCR Windows'un yerleşik motoruyla yapılır (winrt-Windows.Media.Ocr; Türkçe dahil kullanıcının dilleri). Paket
yoksa sadece erişilebilirlik ağacıyla çalışılır.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable

_WORD = re.compile(r"[\wçğıöşüÇĞİÖŞÜ]{2,}", re.U)
MAX_ITEMS = 60
ECHO_SHARE = 0.6  # yazının kelimelerinin bu kadarı hedefte geçiyorsa hedefin yankısıdır (kullanıcının mesajı)


def words(s: str) -> list[str]:
    # İ önce: "ALİCE".lower() "ali̇ce" olur (birleşik nokta) ve "alice" ile eşleşmezdi.
    return [w.replace("İ", "i").replace("I", "i").lower().replace("ı", "i") for w in _WORD.findall(s or "")]


@dataclass
class Item:
    """Ekrandaki tek bir seçilebilir şey. rect: pencere içinde 0-1 kesirli (sol, üst, sağ, alt)."""

    key: str
    source: str  # ax | ocr | ax+ocr | dom
    role: str
    label: str
    rect: tuple[float, float, float, float] | None = None
    value: str = ""
    editable: bool = False
    context: str = ""
    ref: Any = None  # gözlemcinin eylem için kullandığı tutamak (UIA öğe numarası, DOM numarası)

    @property
    def center(self) -> tuple[float, float] | None:
        if self.rect is None:
            return None
        l, t, r, b = self.rect
        return (l + r) / 2, (t + b) / 2

    def where(self) -> str:
        c = self.center
        if c is None:
            return ""
        col = "left" if c[0] < 1 / 3 else "right" if c[0] > 2 / 3 else "middle"
        row = "top" if c[1] < 1 / 3 else "bottom" if c[1] > 2 / 3 else "middle"
        return "center" if col == row == "middle" else f"{row}-{col}"

    def describe(self) -> str:
        kind = "text" if self.source == "ocr" else (self.role or "control")
        s = f"{kind} '{self.label[:80]}'"
        if self.value:
            s += f" (value '{self.value[:40]}')"
        extra = "; ".join(x for x in (self.where(), self.context) if x)
        return s + (f" ({extra})" if extra else "")


@dataclass
class Screen:
    """Bir gözlemin sonucu: pencere, öğeler ve değişimi anlamak için imza."""

    title: str
    location: str
    items: list[Item] = field(default_factory=list)
    focused: str = ""
    image: Any = None  # PIL görüntü (görsel yedek için), yoksa None

    def signature(self) -> str:
        """Ekranın içeriği değişti mi? (öğe adları ve kabaca yerleri)"""
        parts = [self.title] + [f"{i.label}|{i.where()}|{i.value}" for i in self.items]
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def is_goal_echo(text: str, goal: str, echoes: tuple[str, ...] = ()) -> bool:
    """Ekrandaki yazı hedefin kendisi mi (sohbet penceresinde görünen kullanıcı mesajı)? echoes: kullanıcının asıl
    mesajı gibi başka kaynaklar; beyin modeli hedefi yeniden yazınca ekrandaki mesaj hedefle yarı yarıya örtüşüyordu."""
    tw = words(text)
    if len(tw) < 3:
        return False
    for src in (goal, *echoes):
        gw = set(words(src))
        if gw and sum(w in gw for w in tw) / len(tw) >= ECHO_SHARE:
            return True
    return False


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """a'nın ne kadarı b'nin içinde (0-1)."""
    l, t = max(a[0], b[0]), max(a[1], b[1])
    r, bt = min(a[2], b[2]), min(a[3], b[3])
    if r <= l or bt <= t:
        return 0.0
    area = (a[2] - a[0]) * (a[3] - a[1])
    return ((r - l) * (bt - t)) / area if area > 0 else 0.0


def build_items(ax: list[Item], ocr: list[tuple[str, tuple[float, float, float, float]]], goal: str,
                limit: int = MAX_ITEMS, echoes: tuple[str, ...] = ()) -> list[Item]:
    """Erişilebilirlik öğeleri + OCR satırları -> numaralı, tekrarsız, hedefe göre sıralı liste."""
    items: list[Item] = []
    for i in ax:
        if not (i.label or i.editable):
            continue
        # Aynı kontrol ağaçta iç içe iki kez görünebiliyor (Gezgin'in iki 'Address Bar'ı): iki seçenek oyu bölüyordu.
        if i.rect and any(o.role == i.role and o.label.strip().lower() == i.label.strip().lower() and o.rect
                          and _overlap(i.rect, o.rect) > 0.8 and _overlap(o.rect, i.rect) > 0.8 for o in items):
            continue
        items.append(i)
    for text, rect in ocr:
        text = " ".join(text.split())
        if not text or is_goal_echo(text, goal, echoes):
            continue
        # OCR satırı bir kontrolün içinde ve aynı yazıysa ayrı seçenek olmasın: kontrol "ax+ocr" olur.
        host = next((i for i in items if i.rect and i.source != "ocr" and _overlap(rect, i.rect) > 0.6
                     and (not i.label or set(words(text)) & set(words(i.label)))), None)
        if host is not None:
            if not host.label:
                host.label = text
            host.source = "ax+ocr" if host.source == "ax" else host.source
            continue
        items.append(Item("", "ocr", "text", text, rect))
    # Aynı ad birden çok yerdeyse hangisi olduğu satırından anlaşılsın.
    by_label: dict[str, list[Item]] = {}
    for i in items:
        by_label.setdefault(i.label.strip().lower(), []).append(i)
    for same in by_label.values():
        if len(same) < 2:
            continue
        for i in same:
            if i.rect is None:
                continue
            # Pencereyi kaplayan öğe (WebView'in Document'ı) her satırın "komşusu" olurdu.
            row = [o.label for o in items if o is not i and o.rect and o.rect[3] - o.rect[1] < 0.1
                   and o.label.strip().lower() != i.label.strip().lower()
                   and abs(((o.rect[1] + o.rect[3]) / 2) - ((i.rect[1] + i.rect[3]) / 2)) < 0.02][:2]
            if row:
                i.context = "in the row of " + ", ".join(f"'{r[:30]}'" for r in row)
    # Hedefle ilgili olanlar önde, sonra okuma sırası (yukarıdan aşağı, soldan sağa).
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
    """(kullanılabilir mi, neden değilse)"""
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
        _ocr_error = "ekran okuma (OCR) şimdilik sadece Windows'ta"
        raise RuntimeError(_ocr_error)
    try:
        from winrt.windows.media.ocr import OcrEngine
    except ImportError:
        _ocr_error = "OCR paketi kurulu değil: pip install winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging " \
                     "winrt-Windows.Storage.Streams winrt-Windows.Globalization winrt-Windows.Foundation"
        raise RuntimeError(_ocr_error) from None
    eng = OcrEngine.try_create_from_user_profile_languages()
    if eng is None:
        _ocr_error = "Windows'ta OCR dili yok (Ayarlar → Dil → bir dil paketi ekle)"
        raise RuntimeError(_ocr_error)
    _ocr_engine = eng
    return eng


def ocr_image(img: Any, recognize: Callable[[Any], list[tuple[str, tuple[int, int, int, int]]]] | None = None
              ) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Görüntüdeki yazı satırları, kesirli kutularıyla. recognize testte yerine konur."""
    w, h = img.size
    lines = (recognize or _recognize_windows)(img)
    out = []
    for text, (x, y, rw, rh) in lines:
        out.append((text, (x / w, y / h, min(1.0, (x + rw) / w), min(1.0, (y + rh) / h))))
    return out


def _wait(op: Any) -> Any:
    """WinRT IAsyncOperation'ı bekle. Coroutine değil, sadece await edilebilir; asyncio.run onu kabul etmez. Çağıran
    zaten bir olay döngüsünün içindeyse (çekirdek) asyncio.run patlar: o zaman ayrı bir iş parçacığında beklenir."""
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
    """Windows.Media.Ocr: satır metni ve piksel kutusu (x, y, en, boy). Motor en fazla max_image_dimension piksel
    kenar okuyor: daha büyük (3440 genişlik) pencere küçültülür, kutular geri ölçeklenir."""
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    from winrt.windows.media.ocr import OcrEngine

    eng = _engine()
    limit = int(getattr(OcrEngine, "max_image_dimension", 0) or 0) or 4000  # statik: örnekte yok
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
