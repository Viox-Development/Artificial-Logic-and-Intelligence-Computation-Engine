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
    "slash_labels": ("Kod/terminal süsü etiketler (\"AETH-SYS // BRİFİNG-01\", \"SEC-02 // ...\")",
                     "Bölüm üstü etiketleri kaldır ya da sade bir küçük başlık yap (\"Platformlar\"). '//' ve kod "
                     "numaraları teknik görünmez, şablon gibi görünür.", 2),
    "prompt_buttons": ("Düğme/bağlantı metni \">\", \"›\", \"→\" ile başlıyor (\"> PLATFORMLARI İNCELE\")",
                       "Düğme metni düz bir fiil olsun (\"Platformları incele\"); ok gerekiyorsa metnin sonunda küçük "
                       "bir simge.", 1),
    "stat_counters": ("Sayaç/istatistik satırı (\"128.400+\", \"%99,2\", \"640+\")",
                      "Dört sayılık istatistik şeridi en bilinen yapay zekâ kalıbı. Kaldır ya da tek bir sayıyı "
                      "bir cümlenin içinde, bağlamıyla kullan.", 3),
    "fake_annotations": ("Sahte teknik notlar (\"ŞEKİL 01\", \"ÇİZİM NO\", \"REV: D\", \"BRİFİNG\")",
                         "Dekor amaçlı uydurma teknik etiketleri kaldır; görselin yanında gerçekten bilgi veren bir "
                         "açıklama yaz ya da hiçbir şey yazma.", 2),
    "glow": ("Neon parlama gölgeleri (0 0 Npx box/text-shadow, drop-shadow)",
             "Parlama efektlerini kaldır; derinlik gerekiyorsa yumuşak, renksiz, kısa bir gölge ya da hiç.", 2),
    "gradient_text": ("Gradyanlı yazı (background-clip: text)",
                      "Başlıkları düz renkle yaz; vurguyu boyut, kalınlık ve boşlukla ver.", 1),
    "glass": ("Buzlu cam efekti (backdrop-filter: blur) çok yerde",
              "Buzlu cam en fazla sabit üst menüde; kartlarda ve bölümlerde kaldır.", 2),
    "techno_bg": ("Izgara/tarama çizgisi/radar/parçacık arka planları",
                  "Süs arka planlarını kaldır; düz yüzey, güçlü fotoğraf ya da tipografi kullan.", 2),
    "uppercase": ("Her yerde BÜYÜK HARF ve geniş harf aralığı",
                  "Büyük harf + harf aralığını 1-2 küçük yerle sınırla; metnin çoğu normal yazılsın.", 10),
    "mono_font": ("Arayüzde her yerde monospace yazı tipi",
                  "Mono yazı tipi sadece gerçek kod/veri için; başlık, etiket ve düğmelerde normal yazı tipi.", 5),
    "buzzwords": ("Boş slogan ve klişe sözcükler (\"Geleceği şekillendiren\", \"üstünlük\", \"yeni nesil\")",
                  "Sloganları somut cümlelerle değiştir: kim, ne yapıyor, neyi farklı yapıyor (bir bilgi, bir "
                  "rakam, bir örnek).", 3),
    "emoji": ("Simge yerine emoji", "Emojileri kaldır; gerekirse tek tip, ince çizgili SVG simgeler.", 2),
    "ticker": ("Kayan yazı / daktilo efekti / yanıp sönen imleç",
               "Kayan bant ve daktilo efektlerini kaldır; metin sabit dursun.", 1),
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
    hits = {k: Hit(k, t, f) for k, (t, f, _) in RULES.items()}
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
        rows.append(f"- {h.title} — {h.count} yerde. Örnek: {ex}\n  Düzelt: {h.fix}")
    return "\n".join(rows)


def as_dicts(hits: list[Hit]) -> list[dict[str, Any]]:
    return [{"rule": h.rule, "title": h.title, "count": h.count, "examples": h.examples, "fix": h.fix} for h in hits]


CRITIC_PROMPT = """Sen deneyimli bir sanat yönetmenisin. Ekte bir web sitesinin ekran görüntüleri var ({views}).
Proje: {goal}
{design}
Görevin: bu site gerçek, iyi bir tasarım stüdyosunun elinden mi çıkmış, yoksa yapay zekâ ile üretilmiş bir şablon
gibi mi duruyor? Dürüst ve sert ol; nezaket için iyi deme.

Şu kalıplar yapay zekâ işaretidir: kod/terminal süsü etiketler ("XX // YY"), ">" ile başlayan düğmeler, sayaç
satırları ("128.400+"), sahte teknik notlar ("ŞEKİL 01"), neon parlamalar, gradyan yazı, koyu lacivert üstüne tek
neon vurgu rengi, her yerde büyük harf ve mono yazı, boş sloganlar, kutu içinde küçük çizimler, her bölümde aynı kart
ızgarası, fotoğraf yerine soyut süsler.

Türkçe ve kısa yaz:
1. İlk izlenim (tek cümle) ve 1-10 arası puan: özgünlük, görsel hiyerarşi, tipografi, boşluk/düzen.
2. En önemli 5 sorun: her biri hangi görüntüde nerede, neden kötü, somut olarak nasıl düzeltilir (renk, boyut,
   yerleşim, içerik). Genel tavsiye değil, uygulanabilir talimat.
3. Korunması gereken 1-2 iyi şey (varsa)."""


STUDY_PROMPT = """Sen deneyimli bir sanat yönetmenisin ve bir tasarım ekibi için referans araştırması yapıyorsun.
Ekte gerçek bir sitenin ekran görüntüleri var: {url} ({views}; "uzun" sayfanın aşağısını da gösterir). Çerez
penceresi ya da açılır pencere varsa yok say.
Bizim projemiz: {goal}
{note}
Bu sitenin tasarımını somut ve ölçülebilir biçimde çöz (sıfat değil gözlem yaz). Türkçe, kısa maddeler:
1. Genel his ve kime konuşuyor (tek cümle).
2. Düzen: ızgara, kenar boşlukları, ilk ekranda ne var, bölümler nasıl sıralanıyor, boşluk ritmi (bölümler arası
   kabaca kaç px), simetri/asimetri.
3. Tipografi: başlık yazı tipi türü (serif/sans/grotesk, dar/geniş), ağırlık, ana başlığın yaklaşık boyutu (px) ve
   satır yüksekliği, gövde metni boyutu, büyük harf kullanımı, başlık uzunluğu (kaç sözcük).
4. Renk: zemin, metin, vurgu renkleri (yaklaşık hex) ve oranları (ör. %80 beyaz, %15 koyu, %5 vurgu).
5. Görsel dil: fotoğraf mı çizim mi, kadraj, ne gösteriyor; süsleme var mı.
6. Metin tonu: ana başlığı ve bir alt başlığı aynen yaz; nasıl konuşuyor.
7. Bunu iyi yapan 3 şey: bizim siteye nasıl uygulanır (somut talimat).
8. Kaçınılacak 1 şey (varsa)."""


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
                               note=f"Özellikle bak: {note}\n" if note.strip() else "")
    return _vision_chat(router, text, list(shots.values()), max_tokens=1800)


def critique(router, shots: dict[str, str], goal: str, design: str = "", lint_text: str = "",
             refs: dict[str, str] | None = None) -> str:
    """Have a model that can see screenshots critique them. refs: screenshots of the reference sites from
    the research (for comparison, at most 2). Empty if there is no model or on error."""
    if not shots:
        return ""
    refs = dict(list((refs or {}).items())[:2])
    text = CRITIC_PROMPT.format(views=", ".join(shots), goal=goal[:600],
                                design=f"Tasarım yönü belgesinden özet:\n{design[:1500]}\n" if design.strip() else "")
    if refs:
        text += ("\n\nSondaki görüntüler ekibin örnek aldığı referans siteler (" + ", ".join(refs) + "). Bizim siteyi "
                 "onlarla karşılaştır: aynı kalitede mi, aradaki en büyük fark ne?")
    if lint_text:
        text += f"\n\nKoddan otomatik bulunan kalıplar (görüntüde de kontrol et):\n{lint_text[:2000]}"
    return _vision_chat(router, text, list(shots.values()) + list(refs.values()))
