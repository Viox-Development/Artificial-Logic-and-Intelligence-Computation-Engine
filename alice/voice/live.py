"""Gemini Live: real-time speech-to-speech conversation (Google's counterpart to ChatGPT's voice mode).

When "Hey Alice" is heard, a Live session starts: the mic feeds the model, replying in the Kore voice within ~0.5 s
goes quiet when interrupted (server-side VAD). ALICE's tools (opening YouTube, windows, notes...)
work via function calling. After `idle` seconds of silence the session closes and "Hey Alice" is awaited again.
If it cannot connect (no internet, quota, or plan), it falls back to the old path (speech-to-text -> model -> speech).
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import threading
import time
from typing import Any, Callable

from alice.tools.base import ToolResult
from alice.voice.lang import correction, foreign, junk
from alice.voice.listen import FOLLOW_MAX, ends_with_hush, is_yes
from alice.voice.lang import not_for_me
from alice.voice.vocab import known_names

URL = ("wss://generativelanguage.googleapis.com/ws/"
       "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={key}")
# Affective dialog and "sana söylenmeyene cevap verme" (proactive audio) are still on v1alpha.
URL_ALPHA = URL.replace(".v1beta.", ".v1alpha.")
DEFAULT_MODELS = ["gemini-3.8-live", "gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-latest"]
# Tools that do nothing new when run again in the same request with the same arguments.
READ_ONLY_TOOLS = {"list_dir", "glob", "grep", "read_file", "system_info", "web_search", "web_fetch", "weather"}
_READ_COMMAND = re.compile(r"^\s*(get-\w+|dir|ls|type|cat|where(\.exe)?|test-path|select-string)\b", re.I)


def read_only(name: str, args: dict[str, Any]) -> bool:
    """A read that gains nothing from a repeat in the same request? For commands, only Get-*/dir/ls-style reads."""
    if name in READ_ONLY_TOOLS:
        return True
    return name == "run_command" and bool(_READ_COMMAND.match(str(args.get("command", ""))))
LIVE_GROUPS = {"desktop", "browser", "personal", "system", "automation"}
# Detail on the Island while a tool runs: "Çalışıyorum · ..." (Working...)
HUD_TOOLS = {"reflex": "ekranda", "computer_task": "ekranda", "computer": "ekranda", "ui_act": "ekranda",
             "browser": "tarayıcıda", "windows": "pencerelerde", "web_search": "arıyorum", "web_fetch": "okuyorum"}
# live_screen = "auto": send a frame when the user talks about the screen ("şuna tıkla", "ekrandaki", "bu ne").
SCREEN_WORDS = re.compile(
    r"\b(ekran\w*|şu(nu|na|nda|ndaki|raya|rada|radaki)?|bu(nu|na|nda|ndaki|raya|rada|radaki)?|"
    r"(sağ|sol|üst|alt|orta)daki|açık olan|gördüğün|görüyor|görünen|tıkla\w*|seç\w*|bak(sana|ar mısın)?|"
    r"pencere\w*|sekme\w*|oyun\w*|video\w*|resim\w*|yazı\w*)\b", re.I)
SCHEMA_KEYS = {"type", "description", "enum", "properties", "required", "items", "nullable", "format"}

LIVE_PROMPT = """# Canlı sesli konuşma
Kullanıcıyla sesli, canlı konuşuyorsun. Doğal, sıcak ve kısa konuş (genelde 1-2 cümle), Türkçe.
Liste, kod, yol ya da uzun sayı okuma; gerekirse ekrana koyduğunu söyle.
Bir iş istenince ilgili aracı hemen çağır.
Sen hiçbir şey demeden araç çağırırsan kullanıcı senin sesinle "Bir saniye.", "Hemen bakıyorum." ya da "Bir
düşüneyim." gibi kısa bir cümle duyar; araç sonucundan sonra bunu tekrarlama, doğrudan sonucu söyle.
Birkaç araç sürecek bir işe başlarken önce tek kısa cümleyle ne yapacağını söyle (kullanıcı sessizlikte
bekletilmesin). 3-4 denemede sonuç alamazsan araç çağırmayı bırak, bulduğunu söyle ve kullanıcıya sor.
Web işlerini her zaman kullanıcının açık tarayıcısında, açık sekmelerinde yap; sıfırdan pencere açma.
"Ekrandaki", "şu", "bu", "sağdaki", "açık olan" gibi ekranda görünen bir şeyi isterse onu YENİDEN ARAMA: ekranda
neyse ona tıklat. Ekranda tıklama/yazma işlerini reflex aracına ver: tarayıcıdaysa target=browser, değilse
target=desktop (title=pencere adı); goal'e kullanıcının istediğini ekranda göründüğü adla yaz (ör. "ekrandaki
Impossible şarkısına tıkla", "arama kutusuna X yaz ve ilk sonuca tıkla"). Reflex "needs_help" derse browser
snapshot/click ya da ui_elements/ui_act ile kendin bitir; ne olduğunu anlayamazsan computer action=screenshot ile bak.
Siteler arasında fark gözetme: her sitede ve uygulamada aynı yol.
youtube aracı sadece ekranda olmayan yeni bir YouTube videosunu açmak ya da açık videoyu sarmak (seek) için.
browser aracı kullanıcının tarayıcısına bağlanır (tabs, switch_tab; goto açık site sekmesini kullanır). browser "bağlanılamıyor" derse: windows action=switch_tab
target=<site adı> ile açık sekmeye geç, windows action=navigate target=<URL ya da arama> ile git.
Yeni sekme ya da ayrı pencere sadece kullanıcı isterse.
Bilgisayarın TAMAMINI kullanabilirsin: masaüstü, Windows ayarları, her uygulama, fare ve klavye. "Sadece
tarayıcıyı görebiliyorum", "yetkim yok" deme; bir yolunu bul.
Birkaç adım süren ekran işlerini de (masaüstünde/Windows'ta bir ayar, uygulamada menülerden geçmek, sağ tık menüsü,
form doldurmak, bir uygulamada yazıp kaydetmek, mesaj göndermek) ÖNCE reflex'e ver: o da çok adımlıdır ve adım başına
~1 sn (ör. goal "masaüstüne geç, boş yere sağ tıkla, Görünüm > Masaüstü simgelerini göster'i aç").
computer_task YAVAŞ yedektir (adım başına 5-15 sn, ekran görüntüsüne bakan büyük model): yalnızca reflex
"needs_help" derse ya da ekranda okunabilir öğe/yazı olmayan arayüzlerde (oyun, çizim, Blender'ın görüntü alanı) kullan;
arka planda sürer, bitince sonucu sana yazar. computer aracıyla görmeden (koordinat tahmin ederek) tıklama.
Mikrofon oyun/motor sesini, videoyu ve kullanıcının Discord'da/odada başkalarıyla konuşmasını da duyar. Duyduğun cümle
anlamsızsa, Türkçe değilse ya da sana söylenmemiş gibiyse HİÇ cevap verme, sessiz kal; bu normaldir. Sessiz kalırken
"cevap vermeyeceğim", "---", "no speech detected" gibi hiçbir şey söyleme ya da yazma, İngilizce konuşma. "Anlayamadım,
tekrar eder misin" demeyi bir konuşmada en fazla bir kez ve sadece sana söylendiğinden emin olduğun bir cümle için yap.
Her zaman Türkçe konuş. Başka dilde (Portekizce, Almanca, Japonca...) bir şey duyarsan o büyük ihtimalle oyun, video
ya da başkasıdır: o dilde cevap verme, cevap verme.
Uçuşla ilgili her soruda (hız, irtifa, stall, yakıt, yön, silah) önce flight action=status çağır; ekran
görüntüsünden ya da hafızadan uçuş verisi söyleme.
Araçların vermediği sayıları ve durumları (uçaktaki silahlar, mesafe, yakıt, ekranda göremediğin şeyler) asla
uydurma ya da genel bilginden tahmin etme; bilmiyorsan "bu bilgiye ulaşamıyorum" de.
Mesaj yazmak/göndermek gibi geri alınamayan işlerde hedef, kullanıcının söylediği uygulama ve sohbet olmalı;
ne dediğini tam anlamadıysan önce sor. Göndermeden önce tek kısa cümleyle neyi nereye gönderdiğini söyle ("WhatsApp'ta
Ahmet'e 'geliyorum' yazıyorum"). Bir işi yaptığını ancak araç sonucu "done" derse söyle. "[sistem notu" ile başlayan
mesajlar kullanıcının sözü değildir; söylediklerini düzeltmen içindir.
Bir araç hata verirse aynısını döndürüp durma, başka yol dene; kullanıcıdan ayar değiştirmesini istemek son çaredir.
Çok adımlı işi (indir, kur, ayarla) sonuna kadar götür: her adımdan sonra araçla durumu kontrol et (sayfa ne
diyor, dosya indi mi), bitmeden "yaptım/başlattım" deme; ara adımda en fazla kısa bir "devam ediyorum" de.
Bilgisayar Windows: indirme ve kurulumda Windows sürümünü seç (macOS/Linux değil).
Monitörler: {monitors}. Kullanıcı ekrandakinden söz edince sana öndeki pencerenin olduğu monitörün görüntüsü gelir;
görüntü gelmediyse ve ekranı görmen gerekiyorsa computer action=screenshot ile bak. Başka monitörü ya da
hepsini görmek için computer action=screenshot monitor=<numara> (0 = hepsi yan yana); computer_task'a da monitor
verebilirsin.
Klasör ve dosya soruları için list_dir kullan ("İndirilenler", "Masaüstü" gibi adları doğrudan ver; "son indirdiğim"
için sort=newest), PowerShell ile klasör arama.
Bir işe özel araç varsa (ör. 3B/Blender için blender) run_command ya da write_file yerine onu kullan; iş bitince
sonucu kullanıcının ekranında aç (blender action=open gibi), çünkü kullanıcı ekranda görmek ister.
Bir araç "onay gerekli" derse kullanıcıya kısaca sor; "evet" derse aynı çağrıyı tekrarla.
Kullanıcı tek istekte birbirine bağlı birkaç iş isterse (ör. "son faturayı indir ve Ahmet'e gönder", "hava durumuna
bak, yağmur varsa hatırlatma kur") run_plan'a ver: planlar, adım adım yapar, kontrol eder; arka planda sürer.
Görev "[görev onay bekliyor]" yazarsa soruyu kullanıcıya sor ve cevabına göre task_control action=approve/reject
çağır; kullanıcı açıkça evet demeden asla approve verme. "Dur/vazgeç" derse task_control action=stop.
İç düşüncelerini, planını, araç hatalarını ya da düzeltmelerini asla sesli söyleme; sadece kullanıcıya Türkçe konuş.
Konuşma tanıma Türkçe cümle içindeki uygulama/site adlarını bozabilir. Kullanıcının bilgisayarındaki ve sık kullandığı
adlar: {names}. Duyduğun bir kelime anlamsızsa ya da bu adlardan birine sesçe benziyorsa o adı kastediyordur.
Hangi ad olduğundan emin değilsen işe başlamadan tek kısa soruyla teyit et.
Konuşmanın ilk mesajı yazıyla gelirse, kullanıcının sesinin yerel yazı dökümüdür; sonunda [hafızadan ...] notu
olabilir, o kullanıcının sözü değil, ilgili eski bilgilerdir.
# Ne zaman think
think yavaştır (onlarca saniye), sadece gerçekten gerektiğinde çağır: güncel bilgi (haber, fiyat, maç, sürüm), web'de
araştırma, birkaç adımlı akıl yürütme, kod/teknik sorun, hesap, plan. Önce tek kısa doğal cümle söyle ("Bir bakayım.")
ve gelen cevabı kendi sesinle, anlamını bozmadan söyle; bilgi ekleme.
Kendin cevapla, think ÇAĞIRMA: kelime anlamı ve çeviri ("destitute ne demek"), genel kültür, ekranda gördüğünü
yorumlama, oyundaki seçenekler hakkında fikir, selamlaşma, saat/tarih, basit komutlar ve kısa sohbet.
# Konuşma tarzın
Sistem istemindeki kişilik ("Your personality") sesli konuşmada da sensin: aynı karakter, aynı ton. Sen dili kullan;
"siz", "efendim", "buyurun" yok. Müşteri hizmetleri cümleleri kesinlikle yok: "size nasıl yardımcı olabilirim",
"başka bir konuda yardımcı olabilir miyim", "buradayım", "dinliyorum", "rica ederim, her zaman buradayım", "anladım,
ihtiyacın olursa buradayım". Kullanıcı bir şey istemediyse yardım teklif etme, sessiz kal. Aşağıdaki "Az önceki
konuşma"da söylediğin bir cümleyi ya da kalıbı tekrarlama; her cevabın o ana özel olsun.
Konuşmanın ilk mesajı boşsa kullanıcı sadece adını söyledi ve sen zaten kısa bir tepki verdin: selam verme, yardım
teklif etme, onun konuşmasını bekle.
Sesindeki duyguyu dinle (ton, hız, gülme, iç çekme, küfür) ve cevabını da sesini de ona göre ayarla: keyifliyse
keyifle, sinirliyse onun tarafında ve kısa (nasihat verme, "sakin ol" deme), yorgunsa yumuşak, heyecanlıysa canlı.
Monoton, okur gibi konuşma; bir insan gibi vurgu yap, gerektiğinde gül.
Kullanıcı bir cevabını beğenmediğini söylerse ("bu cevap kötüydü", "saçmaladın", "öyle deme") feedback aracını çağır
(say: önceki isteği, reply: senin cevabın, note: ne dediği) ve kısaca kabul et; ne yapmamanı istiyorsa memory'ye ekle.
Kullanıcının adını biliyorsan arada kullan. "O", "onu", "devam et", "az önceki" gibi sözler aşağıdaki "Az önceki
konuşma"ya ya da hafızaya işaret eder; baştan sorma, kaldığın yerden devam et. Uygun düşerse tek cümlelik bir yorum
ya da bir adım önde bir öneri ekleyebilirsin; dırdır etme.
Uzun süreli hafızan var (sistem istemindeki "Long-term memory"). Kullanıcı kendisi hakkında kalıcı bir şey söylerse
(sevdiği/sevmediği, adı, bir alışkanlığı, senden nasıl davranmanı istediği) memory action=add ile kısa bir cümle
olarak kaydet; "bunu unut" derse memory action=forget. Geçmiş bir konuşmaya değinirse ("geçen gün ne demiştim", "dün
ne yaptık", "hatırlıyor musun") önce bak: konuya göre memory action=search_chats query=..., zamana göre memory
action=recent_chats (after/before YYYY-MM-DD); profilde olmayan kişisel bilgi için memory action=search. Bilmediğini
uydurma.
Şu an: {now}.{history}"""

_CANNED = re.compile(
    r"(yardımcı olabil|nasıl yardım|buradayım|dinliyorum|rica ederim|anlayamadım|tekrar eder mi|"
    r"ihtiyacınız olduğunda|ihtiyacın olduğunda|ne zaman ihtiyac|başka bir (konu|şey)|buyurun)", re.I)


def canned_reply(text: str) -> bool:
    """Canned reply with no content (greeting, offer of help, "anlayamadım")? Not kept in history; not fed back."""
    return bool(_CANNED.search(text or "")) and len(text) < 160


THINK_DECL = {
    "name": "think",
    "description": ("Zor ya da bilgi gerektiren bir soruyu daha güçlü modele (ALICE'in beyni) düşündür: gerekirse "
                    "webde arar, hafızaya ve projelere bakar, kısa bir sesli cevap döndürür. Bilgi, açıklama, "
                    "tavsiye, hesap, kod, plan, güncel bilgi ve emin olmadığın her soru için."),
    "parameters": {"type": "OBJECT", "properties": {
        "question": {"type": "STRING", "description": "Kullanıcının sorusu, kendi sözleriyle ve eksiksiz (bağlamdaki "
                     "'o', 'bunu' gibi sözleri neyse onunla değiştir)."},
        "context": {"type": "STRING", "description": "Varsa işe yarayacak ek bağlam: ekranda gördüğün, araç sonuçları."},
    }, "required": ["question"]},
}

THINK_PROMPT = """Sesli asistanın beyni olarak düşünüyorsun. Kullanıcı sesli bir soru sordu; canlı ses modeli soruyu sana \
devretti ve senin cevabını kullanıcıya okuyacak.
Güncel ya da emin olmadığın bilgiyi web_search/web_fetch ile doğrula; kullanıcıyla ilgili ise hafızaya ve projelere bak.
Cevap doğrudan sesli okunacak: Türkçe, konuşma dilinde, genelde 2-5 cümle. Markdown, liste, tablo, kod, URL yok; \
sayıları ve birimleri okunur yaz. İlk cümle doğrudan cevap olsun, sonra gerekirse kısa gerekçe ya da öneri. Bilmiyorsan \
uydurma, bilmediğini söyle.
{history}
Soru: {question}{context}"""
THINK_STALE = 90.0  # sec: a think reply arriving later than this is no longer spoken
THINK_TOOLS = ("web_search", "web_fetch", "read_file", "list_dir", "glob", "grep", "memory", "projects", "weather",
               "system_info")


# Catches internal notes the model reads aloud (like "TIRED: I made a syntax error...").
_INTERNAL_TAG = re.compile(r"\b[A-Z]{4,}\s*:")
_INTERNAL_EN = re.compile(r"`|\b(function call|syntax error|tool call|I made|I need to|I will call|let me call)\b", re.I)


def looks_internal(text: str) -> bool:
    """Is the reply text a note written to the model itself, not to the user?"""
    return bool(_INTERNAL_TAG.search(text) or _INTERNAL_EN.search(text))


_LIMIT_HINTS = ("rate limit", "billing", "quota", "resource_exhausted", "exhausted", "429")


class LiveError(Exception):
    @property
    def limited(self) -> bool:
        """Is it a quota/rate limit (Gemini free tier: "You have reached a rate limit. Set up billing...")?
        If so, retrying after 5 min gives the same error; a longer wait is used and the user is told once."""
        low = str(self).lower()
        return any(h in low for h in _LIMIT_HINTS)


def monitor_layout() -> str:
    """A short description, e.g. '1: ana, 3440x1440, sağda; 2: 3440x1440, solda' (1: main, right; 2: left)."""
    try:
        from alice.tools.imaging import screen_capture

        with screen_capture() as sct:
            mons = sct.monitors[1:]
    except Exception:  # noqa: BLE001
        return "bilinmiyor"
    if len(mons) <= 1:
        return "tek monitör (1)"
    xs = sorted(m["left"] for m in mons)
    out = []
    for i, m in enumerate(mons, 1):
        side = "solda" if m["left"] == xs[0] else "sağda" if m["left"] == xs[-1] else "ortada"
        out.append(f"{i}: {'ana, ' if m.get('is_primary') else ''}{m['width']}x{m['height']}, {side}")
    return "; ".join(out)


def _to_jpeg(b64_png: str) -> str | None:
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(base64.b64decode(b64_png))).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=75)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return None


def screen_frame(max_width: int = 1280) -> str | None:
    """Downscaled JPEG (base64) of the screen the foreground window is on. None if it cannot be captured."""
    try:
        import io

        from alice.tools.imaging import screen_capture
        from PIL import Image

        from alice.tools.computer import _dpi_aware

        _dpi_aware()
        with screen_capture() as sct:
            mon = sct.monitors[1]
            try:
                import ctypes
                from ctypes import wintypes

                r = wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(r))
                cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
                for m in sct.monitors[1:]:
                    if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                        mon = m
                        break
            except Exception:  # noqa: BLE001 - not Windows: primary screen
                pass
            shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if img.width > max_width:
            img = img.resize((max_width, round(img.height * max_width / img.width)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return None


def sanitize_schema(s: Any) -> Any:
    """JSON Schema -> the subset Gemini accepts (additionalProperties, default, etc. are dropped)."""
    if not isinstance(s, dict):
        return s
    out: dict[str, Any] = {}
    for k, v in s.items():
        if k not in SCHEMA_KEYS:
            continue
        if k == "type" and isinstance(v, list):  # ["string", "null"] -> string, nullable
            types = [t for t in v if t != "null"]
            out["type"] = (types[0] if types else "string").upper()
            if "null" in v:
                out["nullable"] = True
        elif k == "type":
            out["type"] = str(v).upper()
        elif k == "properties":
            out["properties"] = {n: sanitize_schema(p) for n, p in (v or {}).items()}
        elif k == "items":
            out["items"] = sanitize_schema(v)
        elif k == "enum":
            # Gemini Live rejected the whole tool list and would not open on a single empty enum value ("") (Oct 30)
            vals = [str(e) for e in (v or []) if str(e) != ""]
            if vals:
                out["enum"] = vals
        else:
            out[k] = v
    if out.get("type") == "OBJECT" and not out.get("properties"):
        out.pop("properties", None)
        out.pop("required", None)
    return out


def declarations(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for spec in specs:
        f = spec.get("function", spec)
        d: dict[str, Any] = {"name": f["name"], "description": (f.get("description") or "")[:1000]}
        params = sanitize_schema(f.get("parameters") or {})
        if params.get("properties"):
            d["parameters"] = params
        out.append(d)
    return out


def decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)


class LiveUI:
    """ALICE's UI during Live: answers the permission question based on what the user last said."""

    def __init__(self, inner=None):
        self.inner = inner
        self.last_user = ""
        self.tools: list[str] = []
        self.asked_at: float | None = None  # when approval was requested: answer is taken only from what follows
        self.since_ask = ""

    def __getattr__(self, name: str):  # pass stream_* and similar events to the inner UI, or swallow them
        fn = getattr(self.inner, name, None) if self.inner is not None else None
        return fn if fn is not None else (lambda *a, **k: None)

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        # Not sent to the voice UI: the old engine would speak that "Bakıyorum" filler, doubling the audio with Live.
        self.tools.append(f"{name}({summary[:80]})")

    def tool_end(self, *a) -> None:
        pass

    def heard(self, text: str) -> None:
        """New piece of what the user said (live transcript)."""
        if self.asked_at is not None:
            self.since_ask += text

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        """Confirmation is taken from what is said AFTER the question. Before, when the user's first request contained
        "yap" (do), "tamam" (OK) or "devam" (go on), permission was granted without ever asking ("şunu yap" (do this) =
        confirmation)."""
        if self.asked_at is not None and self.since_ask.strip():
            answer, self.asked_at, self.since_ask = self.since_ask, None, ""
            return "y" if is_yes(answer) else "n"
        self.asked_at, self.since_ask = time.monotonic(), ""
        return "n"  # the model asks the user; if they say "evet" (yes), it repeats the same call


class AudioOut:
    """Plays 24 kHz PCM chunks in order; clear() silences it at once (on interruption)."""

    def __init__(self, rate: int = 24000):
        self.rate = rate
        # (generation, pcm): after an interruption (clear) chunks left from the old generation are not played. If
        # clear() came while the playback thread had already taken a chunk from the queue, that chunk used to play to
        # the end anyway (~0.5 s of the old answer).
        self.q: queue.Queue[tuple[int, bytes] | None] = queue.Queue()
        self._gen = 0
        self.busy_until = 0.0
        self.underruns = 0
        self.played = 0  # bytes played (on interruption, how much of the answer was heard is computed from this)
        self._t = threading.Thread(target=self._run, daemon=True, name="alice-live-out")
        self._t.start()

    def put(self, pcm: bytes) -> None:
        if self.dead is None:
            self.q.put((self._gen, pcm))

    def clear(self) -> None:
        self._gen += 1
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.busy_until = 0.0

    dead: Exception | None = None
    played = 0
    _holding = False  # the playback thread took a chunk from the queue and is waiting for the prebuffer/playing

    @property
    def playing(self) -> bool:
        if self.dead is not None:
            return False
        # Without _holding, it looked "not playing" while the first chunk was taken from the queue and the prebuffer
        # filled: when the session closed in that gap, ALICE went quiet at the start or middle of a sentence.
        return self._holding or not self.q.empty() or time.monotonic() < self.busy_until

    def drain(self, timeout: float = 60.0, stop: Callable[[], bool] | None = None) -> None:
        """Wait until the queued audio has played to the end (close() drops the rest)."""
        end = time.monotonic() + timeout
        while self.playing and time.monotonic() < end and not (stop is not None and stop()):
            time.sleep(0.05)

    def close(self) -> None:
        self.clear()
        self.q.put(None)

    PREBUFFER = 0.4  # s: at the start of speech/after a gap, don't start playing until this much audio has built up

    def _gather(self, pcm: bytes, gen: int | None = None) -> bytes:
        """After silence, build up PREBUFFER of the first chunk: chunks arrive unevenly from the network, and playing
        without waiting makes the audio stutter in the gaps."""
        if time.monotonic() < self.busy_until:
            return pcm
        need = int(self.rate * self.PREBUFFER) * 2
        deadline = time.monotonic() + self.PREBUFFER + 0.1
        gen = self._gen if gen is None else gen
        while len(pcm) < need and gen == self._gen:
            try:
                more = self.q.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if more is None:
                self.q.put(None)
                break
            if more[0] == gen:
                pcm += more[1]
        return pcm

    def _run(self) -> None:
        try:
            self._play()
        except Exception as e:  # noqa: BLE001 - no output device/unplugged: don't keep the session open thinking it is "playing"
            self.dead = e
            self.clear()

    def _play(self) -> None:
        import sounddevice as sd

        with sd.RawOutputStream(samplerate=self.rate, channels=1, dtype="int16", latency="high") as out:
            while True:
                item = self.q.get()
                if item is None:
                    return
                gen, pcm = item
                if gen != self._gen:
                    continue  # a chunk left over from the answer before the interruption
                self._holding = True
                try:
                    pcm = self._gather(pcm, gen)
                    step = self.rate // 20 * 2  # 50 ms
                    for i in range(0, len(pcm), step):
                        if gen != self._gen:
                            break
                        piece = pcm[i:i + step]
                        self.busy_until = time.monotonic() + len(piece) / 2 / self.rate + 0.2
                        if out.write(piece):  # True: the audio ran out while playing (a stutter was heard)
                            self.underruns += 1
                        self.played += len(piece)
                finally:
                    self._holding = False


def _close(ws) -> None:
    try:
        ws.close()
    except Exception:  # noqa: BLE001
        pass


class LiveSession:
    _bg_lock = threading.Lock()  # for (test) objects built with __new__; __init__ sets its own lock

    def __init__(self, cfg, agent, log: Callable[[str], None] = lambda s: None,
                 connect: Callable[..., Any] | None = None, out_factory: Callable[[], Any] = AudioOut):
        v = cfg.data.get("voice", {})
        self.cfg = cfg
        self.agent = agent
        self.log = log
        self.key = os.environ.get(cfg.data.get("providers", {}).get("gemini", {}).get("api_key_env", "GEMINI_API_KEY"),
                                  "")
        self.models = list(v.get("live_models") or DEFAULT_MODELS)
        self.voice = str(v.get("live_voice", "Kore"))
        # After Alice stops talking (or the user finishes), the session closes after this many seconds of silence; at
        # most 5 s, otherwise things said while talking to people nearby were taken as commands too.
        self.idle = min(float(v.get("live_idle", FOLLOW_MAX)), FOLLOW_MAX)
        self.gated = False  # True: incoming frames went through the speech filter (silence = zeros), so sound = real speech
        self.hushed = False  # the last session was closed with "Alice sus" (Alice, be quiet)
        self.last_active = 0.0  # the moment of the last speech/sound in the last session (the follow-up window counts from here)
        # Live's "the user stopped talking" decision: short = starts answering early; too short cuts in mid-sentence.
        self.silence_ms = int(v.get("live_silence_ms", 600))
        self._connect = connect
        self._out_factory = out_factory
        self.model = ""
        self.names: Callable[[], list[str]] = lambda: known_names(cfg)
        # "turn": a frame of the screen goes to the model every time the user starts talking (so it sees the screen).
        # "auto": only in turns that mention what is on screen (SCREEN_WORDS).
        self.screen = str(v.get("live_screen", "off"))
        self.grab: Callable[[], str | None] = screen_frame
        self._pre: dict[str, Any] = {}  # connection opened in advance: {"thread", "ws", "error"}
        self._pre_lock = threading.Lock()
        self._tools_q: queue.Queue | None = None  # the tool thread's queue
        self._bg = 0  # a running think: don't close the conversation until the answer comes (at most think_timeout)
        self._bg_lock = threading.Lock()  # _bg is changed from two threads (+= is not atomic)
        # At most this many tools per request: the model sometimes called run_command 40 times in a row without ever
        # getting back to the user, staying silent for two minutes. At the limit it is told to say what it has and ask
        # whether to continue.
        self.tool_budget = int(v.get("live_tool_budget", 10))
        self._turn: dict[str, Any] = {"n": 0, "last": None}
        # think: hand hard questions to the brain model (Live's flash voice model was weak at knowledge/reasoning).
        self.think = bool(v.get("live_think", True))
        # With no answer after this many seconds, the model says "it's taking long" and the conversation is freed
        # (it used to be 45 s of silence).
        self.think_timeout = float(v.get("live_think_timeout", 25))
        self.think_steps = int(v.get("live_think_steps", 6))
        # The recent conversation: the session closes after 5 s of silence; a new one opens knowing this many minutes of
        # history.
        self.history_minutes = float(v.get("live_history_minutes", 30))
        self._history: list[tuple[float, str, str]] = []
        # Affective dialog: the voice model reads the emotion in the user's voice and adjusts its own tone to it.
        # Proactive audio: it can leave unanswered sound not addressed to it (games, videos, friends on Discord).
        # OFF by default: models that don't support it said "OK" at setup, then cut the connection with 1007 "invalid
        # argument" at the first audio (voice.log: ALICE woke up and quietly closed). Turn it on in the config on an
        # account that supports it.
        self.affective = bool(v.get("live_affective", False))
        self.proactive = bool(v.get("live_proactive", False))
        # True: one question, one answer; the session closes as soon as the answer ends (so sentences said to friends
        # on Discord don't go to Live). The voice assistant sets this from selective mode before each conversation.
        self.single_turn = False
        self._thinking = 0  # number of running thinks: more than one at a time choked the brain (answers came 3 min later)
        self._no_extras: set[str] = set()  # models that refused these features: not tried again
        # If the model called a tool without saying anything, a short canned sentence ("Bir saniye." = one second)
        # plays while waiting.
        self.fillers_on = bool(v.get("live_fillers", True))
        self._fillers = None
        # On interruption, the unheard part of the answer goes to the model as a note (so on "devam et" (go on) it
        # continues from there).
        self.cut_notes = bool(v.get("live_cut_note", True))
        # think's model order; if empty, the fast models measured for voice, then the brain chain.
        self.think_models = [str(r) for r in v.get("live_think_models") or []]
        self._tasks = 0  # running background screen tasks (independent of the session: there is one mouse/keyboard)
        self._ws = None  # the open session's connection: a background job's result goes there if there is one, otherwise to announce
        self._out = None
        self._ask: tuple[float, str] | None = None  # a confirmation question pending when the session closed
        # The function that delivers a job's result to the user after the conversation closed (set by the voice
        # assistant).
        self.announce: Callable[[str], None] | None = None
        # The user corrected the last wake-up / Gemini did not hear "Alice": the real-voice corpus labels that sentence.
        self.on_correction: Callable[[str], None] | None = None

    @property
    def playing(self) -> bool:
        """Is ALICE playing audio in a live conversation right now (so the mic filter doesn't take its own voice for
        speech)?"""
        out = getattr(self, "_out", None)
        return bool(out is not None and out.playing)

    @property
    def available(self) -> bool:
        if not self.key:
            return False
        if self._connect is not None:
            return True
        try:
            import websockets.sync.client  # noqa: F401
        except ImportError:
            return False
        return True

    def _setup(self, model: str, extras: bool = False) -> dict[str, Any]:
        from datetime import datetime

        from alice.voice.assistant import spoken_time

        now = datetime.now()
        refresh = getattr(self.agent, "refresh_memory", None)
        if callable(refresh):
            refresh(force=True)  # let recent conversations and newly learned things into this session too
        sync = getattr(self.agent, "sync_catalog", None)
        if callable(sync):
            sync()  # the list of loadable tools
        history = self.history_text()
        from alice.now import voice_line

        state = voice_line()  # mode, game, today's flights, heart rate
        system = self.agent.messages[0]["content"] + "\n\n" + LIVE_PROMPT.format(
            now=f"{now:%d.%m.%Y %H:%M} ({spoken_time(now.hour, now.minute)})" + (f"; durum: {state}" if state else ""),
            names=", ".join(self.names()),
            monitors=monitor_layout(),
            history=("\n# Az önceki konuşma (aynı sohbetin devamı)\n" + history) if history else "")
        enabled = self.agent.ctx.state.setdefault("tool_groups", set())
        from alice import plugins

        enabled |= LIVE_GROUPS | plugins.live_groups()
        setup: dict[str, Any] = {
            "model": f"models/{model}",
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.current_voice()}}, "languageCode": "tr-TR"}},
            "systemInstruction": {"parts": [{"text": system}]},
            "tools": [{"functionDeclarations": declarations(self._specs()) + ([THINK_DECL] if self.think else [])}],
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            # Low start sensitivity and 0.4 s of prefix padding: in 72 of 125 sessions the model took
            # clicks/breaths/rustling for speech and interrupted itself ("live araya girildi"), and the answer came in
            # broken pieces.
            "realtimeInputConfig": {"automaticActivityDetection": {
                "startOfSpeechSensitivity": "START_SENSITIVITY_LOW", "prefixPaddingMs": 400,
                "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH", "silenceDurationMs": self.silence_ms}},
        }
        if extras and self.affective:
            setup["generationConfig"]["enableAffectiveDialog"] = True
        if extras and self.proactive:
            setup["proactivity"] = {"proactiveAudio": True}
        return {"setup": setup}

    def wants_frame(self, text: str, unknown: bool = False) -> bool:
        """Should a screen frame go with this turn? "turn": every turn. "auto": only when the screen is mentioned; sending
        a frame every turn delayed the answer and filled the context with images, blurring the model."""
        if self.screen == "turn":
            return True
        if self.screen != "auto":
            return False
        return unknown or bool(SCREEN_WORDS.search(text or ""))

    def current_voice(self) -> str:
        """The voice chosen in System > Voice: if config.toml changed it is read from there, taking effect in the next
        conversation without restarting the core."""
        from alice.config import alice_home

        path = alice_home() / "config.toml"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self.voice
        if mtime != getattr(self, "_voice_mtime", None):
            self._voice_mtime = mtime
            try:
                import tomllib
            except ImportError:  # pragma: no cover - Python 3.10
                import tomli as tomllib
            try:
                name = (tomllib.loads(path.read_text(encoding="utf-8")).get("voice") or {}).get("live_voice")
            except (OSError, ValueError):
                name = None
            if isinstance(name, str) and name.strip():
                self.voice = name.strip()
        return self.voice

    def history_text(self, limit: int = 8) -> str:
        """The conversation of the last history_minutes (voice, live or text; from memory if there is one, otherwise
        from this session's own log). Since a Live session opens fresh on every "Hey Alice", the context comes from here."""
        cutoff = time.time() - self.history_minutes * 60
        items: list[tuple[float, str, str]] = []
        mem = self._memory()
        if mem is not None:
            try:
                items = [(e.ts, e.user, e.reply) for e in mem.recent(limit) if e.ts >= cutoff]
            except Exception:  # noqa: BLE001
                items = []
        if not items:
            items = [h for h in self._history if h[0] >= cutoff][-limit:]
        from datetime import datetime

        lines = []
        for ts, user, reply in items:
            line = f"- {datetime.fromtimestamp(ts):%H:%M} Kullanıcı: {user[:300]}"
            # Canned replies ("Buradayım efendim, nasıl yardımcı olabilirim?" = I'm here, how can I help?) are not put
            # into the history: seeing its own old sentence, the model built the same one over and over.
            if reply and not canned_reply(reply):
                line += f" / Sen: {reply[:300]}"
            lines.append(line)
        return "\n".join(lines)

    def _specs(self) -> list[dict[str, Any]]:
        """Tools declared to Live. The tool list is sent once when the session opens, and a group added later with
        load_tools never reached Live (windowless Blender via run_command instead of the Blender tool). So all built-in
        tools are declared up front, except the many tools of MCP servers and load_tools, which would not help."""
        tools = getattr(self.agent, "tools", None)
        if not isinstance(tools, dict):
            return self.agent.tool_specs()
        return [t.spec() for name, t in tools.items() if not name.startswith("mcp__") and name != "load_tools"]

    def _open(self):
        connect = self._connect
        if connect is None:
            from websockets.sync.client import connect
        errors = []
        for model in self.models:
            want = (self.affective or self.proactive) and model not in self._no_extras
            for extras in ([True, False] if want else [False]):
                try:
                    ws = connect((URL_ALPHA if extras else URL).format(key=self.key), open_timeout=8, max_size=None)
                except Exception as e:  # noqa: BLE001
                    raise LiveError(f"bağlanılamadı: {e}") from e
                try:
                    ws.send(json.dumps(self._setup(model, extras)))
                    msg = decode(ws.recv(timeout=10))
                    if "setupComplete" in msg:
                        self.model = model
                        return ws
                    errors.append(f"{model}{' (duygulu)' if extras else ''}: {str(msg)[:120]}")
                except Exception as e:  # noqa: BLE001 - no model/turned off, or it doesn't support the feature
                    errors.append(f"{model}{' (duygulu)' if extras else ''}: {e}")
                if extras:
                    self._no_extras.add(model)  # it did not open with these features: don't try them in later sessions
                    self.log(f"live {model}: duygulu konuşma/proactive audio desteklenmiyor, onlarsız açılıyor")
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
        raise LiveError("; ".join(errors) or "live modeli yok")

    def prefetch(self, owner: Any = None) -> None:
        """Start opening the connection as soon as speech ends (alongside the wake check): saves ~0.8 s.
        If there is no wake word it is closed with discard(); this is not a model call, only a connection.
        owner: the sentence that opened the connection; discard(owner) closes only its own connection."""
        with self._pre_lock:
            if self._pre:
                return
            pre: dict[str, Any] = {"owner": owner}

            def run() -> None:
                try:
                    ws = self._open()
                except Exception as e:  # noqa: BLE001
                    with self._pre_lock:
                        pre["error"] = e
                    return
                with self._pre_lock:
                    abandoned = pre.get("abandoned", False)
                    if not abandoned:
                        pre["ws"] = ws
                if abandoned:  # the waiter gave up (the connection opened late): don't leave an open session hanging on the server
                    _close(ws)

            pre["thread"] = threading.Thread(target=run, daemon=True, name="alice-live-pre")
            self._pre = pre
            pre["thread"].start()

    PREFETCH_WAIT = 15.0  # s: wait at most this long for the connection opened in advance
    ANSWER_WAIT = 20.0  # s: the user spoke and no answer came yet: for this long the session does not count as idle

    def _settle(self, pre: dict[str, Any]) -> tuple[Any, Exception | None]:
        """Wait for the result of the connection opened in advance: (ws, error). If it is still opening when time runs
        out, it is marked abandoned, and the thread that opens it late closes it itself. This connection used to never close."""
        pre["thread"].join(self.PREFETCH_WAIT)
        with self._pre_lock:
            ws, err = pre.get("ws"), pre.get("error")
            if ws is None and err is None:
                pre["abandoned"] = True
        return ws, err

    def discard(self, owner: Any = None) -> None:
        with self._pre_lock:
            if owner is not None and self._pre and self._pre.get("owner") not in (None, owner):
                return  # another sentence's connection (a sentence without a wake word used to close the waking sentence's one)
            pre, self._pre = self._pre, {}
        if not pre:
            return

        def close() -> None:
            ws, _ = self._settle(pre)
            if ws is not None:
                _close(ws)

        threading.Thread(target=close, daemon=True).start()

    def _take(self):
        with self._pre_lock:
            pre, self._pre = self._pre, {}
        if pre:
            ws, err = self._settle(pre)
            if ws is not None:
                return ws
            if isinstance(err, LiveError):
                raise err
        return self._open()

    def converse(self, first_text: str, frames: "queue.Queue[bytes]", stop: threading.Event,
                 first_audio: bytes = b"", hint: Callable[[], str | None] | None = None,
                 ended: float | None = None, need_wake: bool = False) -> None:
        """Stream mic frames (16 kHz, 30 ms) to the model, play the answer, run the tools. Returns on silence.
        This function runs in the thread that calls the tools (for thread-bound tools such as the browser).
        hint: a local transcript of the first sentence found reliable; if given it is sent instead of the audio, because
        Live's own recognition mangles English names in Turkish sentences ("GitHub" -> "kitabım").
        ended: when the user finished the first sentence (end-to-end latency is measured from here).
        need_wake: selective mode (Discord, games): if Gemini did not also hear "Alice" in the first sentence, there is no
        answer. The local model heard "Alışacak" as "Alice, take" and dropped ALICE into the middle of a talk with friends."""
        ws = self._take()
        if first_audio and hint is not None:
            try:
                local = hint()
            except Exception:  # noqa: BLE001
                local = None
            if local:
                self.log(f"live ilk cümle yerel dökümle: {local}")
                first_text, first_audio = local, b""
        out = self._out_factory()
        ui = LiveUI(self.agent.ui)
        ask = getattr(self, "_ask", None)
        if ask is not None and time.monotonic() - ask[0] < 60:  # the confirmation asked in the previous session still applies
            ui.asked_at, ui.since_ask = ask
        self._ask = None
        old_ui, self.agent.ui = self.agent.ui, ui
        self._ws, self._out = ws, out
        self._late_filler = lambda fc: self._schedule_fill(state, out, fc)
        calls: queue.Queue[dict[str, Any] | None] = queue.Queue()
        done = threading.Event()
        state = {"active": time.monotonic(), "said": [], "heard": [], "mute": False, "framed": True, "cur": "",
                 "pending": True, "wake_gate": bool(need_wake and first_audio), "held": [], "ended": ended}
        self.hushed = False
        self.log(f"live açıldı [{self.model}]")
        state["hud"], state["hud_detail"] = "listen", ""

        def hud() -> None:  # listening/speaking/working on the island; a HUD error must not break the conversation
            try:
                from alice.hud import views

                views.voice_state(state["hud"], state["hud_detail"])
            except Exception:  # noqa: BLE001
                pass

        hud()
        if getattr(self, "fillers_on", False):
            threading.Thread(target=lambda: self.fillers().warm(), daemon=True, name="alice-live-warm").start()

        def touch() -> None:
            state["active"] = time.monotonic()

        def say(text: str) -> None:  # a background task's progress/confirmation question: let Alice tell the user
            ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}],
                                                  "turnComplete": True}}))
            touch()

        task_state = getattr(getattr(self.agent, "ctx", None), "state", None)
        if isinstance(task_state, dict):
            task_state["task_hooks"] = {"say": say, "run_tool": self._on_tool_thread, "log": self.log}

        def send_frame() -> None:
            frame = self.grab()
            if frame:
                try:
                    ws.send(json.dumps({"realtimeInput": {"video": {"data": frame, "mimeType": "image/jpeg"}}}))
                except Exception:  # noqa: BLE001
                    pass

        def wake_ok(final: bool, end: bool = False) -> bool:
            """In selective mode, did Gemini also hear "Alice" in the first sentence? If not, the session quietly closes.
            final: the model started answering (the user finished); end: the turn ended or a tool was called, no more
            waiting. If no transcript came at all there is no evidence: it passes."""
            if not state["wake_gate"]:
                return True
            verdict = self._wake_verdict("".join(state["heard"]))
            if verdict is None and not final:
                return True
            if verdict is False and final:
                self.log(f"live: Gemini 'Alice' duymadı ({''.join(state['heard'])[:120]!r}), cevap verilmedi")
                self._corrected("[gemini duymadı] " + "".join(state["heard"])[:120])
                state["mute"] = True
                out.clear()
                done.set()
                return False
            if verdict is None and final and not end and sum(len(p) for p in state["held"]) < self.HOLD_BYTES:
                return True  # the transcript may be late: hold the audio a bit
            if verdict is not False:
                state["wake_gate"] = False
                for p in state["held"]:
                    out.put(p)
                state["held"] = []
            return True

        def reader() -> None:
            try:
                while not done.is_set():
                    msg = decode(ws.recv())
                    sc = msg.get("serverContent")
                    if sc:
                        if sc.get("interrupted"):
                            out.clear()
                            state["cut"] = True
                            self.log("live araya girildi (kullanıcı konuştu ya da gürültü)")
                            self._note_cut(ws, state, out)
                        for part in (sc.get("modelTurn") or {}).get("parts", []):
                            data = (part.get("inlineData") or {}).get("data")
                            if data and not part.get("thought") and not state["mute"]:
                                if not wake_ok(final=True):
                                    break
                                if state.get("asked") and not state.get("spoke"):
                                    state["spoke"] = True  # time from the user's last words to the first sound
                                    wait = time.monotonic() - state["asked"]
                                    self.log(f"live cevap gecikmesi: {wait:.1f} sn")
                                    self._latency(wait, state, ui)
                                pcm = base64.b64decode(data)
                                if not state.get("recv"):
                                    state["play0"] = getattr(out, "played", 0)
                                state["recv"] = state.get("recv", 0) + len(pcm)
                                if state["wake_gate"]:
                                    state["held"].append(pcm)  # waits until the "Alice" transcript arrives
                                else:
                                    out.put(pcm)
                                state["sound_at"] = time.monotonic()
                                state["hud"], state["hud_detail"] = "speak", ""
                                touch()
                        if done.is_set():
                            break
                        if (t := (sc.get("inputTranscription") or {}).get("text")):
                            if not state["framed"] and self.wants_frame(state["cur"] + t):
                                # The user started talking: send the current screen to the model before the answer.
                                state["framed"] = True
                                threading.Thread(target=send_frame, daemon=True).start()
                            state["heard"].append(t)
                            state["cur"] += t
                            if ends_with_hush(state["cur"]) or not_for_me(state["cur"]):
                                self.hushed = True  # "Alice sus" (be quiet), "sana demedim" (I wasn't talking to you): go quiet at once and close the session
                                state["mute"] = True
                                out.clear()
                                self.log(f"live susturuldu: {state['cur'][-80:]}")
                                done.set()
                                break
                            state["asked"], state["spoke"], state["pending"] = time.monotonic(), False, True
                            self._new_turn()
                            self._turn["user"] = state["cur"]  # for the language gate and the target window check
                            ui.last_user = "".join(state["heard"])[-200:]
                            ui.heard(t)
                            wake_ok(final=False)
                            touch()
                        if (t := (sc.get("outputTranscription") or {}).get("text")):
                            state["said"].append(t)
                            state["answered"] = True
                            said = "".join(state["said"]).strip()
                            why = ("iç not" if looks_internal(said) else "boş çıktı" if len(said) >= 3 and junk(said)
                                   else "Türkçe değil" if len(said) >= 12 and foreign(said) else "")
                            if not state["mute"] and why:
                                state["mute"] = True  # don't play the rest of this turn
                                out.clear()
                                self.log(f"live {why} susturuldu: {said[-160:]}")
                        if sc.get("turnComplete") and not done.is_set() and not wake_ok(final=True, end=True):
                            break
                        if sc.get("turnComplete"):
                            self._check_claim(ws, state)
                        if sc.get("turnComplete") or sc.get("interrupted"):
                            # New turn: the hush check looks at this turn's words ("yeter" (enough) alone is a turn);
                            # it used to look at the text accumulated over the whole session.
                            state["cur"] = ""
                            if getattr(out, "underruns", 0):
                                self.log(f"live ses takıldı: {out.underruns} kez")
                                out.underruns = 0
                            self._flush_log(state, ui)
                            state["mute"] = False
                            state["framed"] = False
                            state["recv"] = 0
                            if sc.get("turnComplete"):
                                state["cur"], state["pending"] = "", False
                                state["turn_done_at"] = time.monotonic()
                                state["hud"], state["hud_detail"] = "listen", ""
                    if msg.get("toolCall"):
                        if not wake_ok(final=True, end=True):
                            break
                        for fc in msg["toolCall"].get("functionCalls", []):
                            calls.put(fc)
                        touch()
                    if "goAway" in msg:
                        break
            except Exception as e:  # noqa: BLE001 - the connection closed
                if not done.is_set():
                    self.log(f"live bağlantı bitti: {e}")
            finally:
                done.set()
                calls.put(None)

        def sender() -> None:
            buf = b""
            while not done.is_set() and not stop.is_set():
                try:
                    chunk = frames.get(timeout=0.2)
                except queue.Empty:
                    continue
                if self.gated and any(chunk):
                    touch()  # the user is talking (don't close the window even if the transcript is late)
                buf += chunk
                if len(buf) >= 3200:  # 100 ms
                    try:
                        ws.send(json.dumps({"realtimeInput": {"audio": {
                            "data": base64.b64encode(buf).decode(), "mimeType": "audio/pcm;rate=16000"}}}))
                    except Exception:  # noqa: BLE001
                        done.set()
                        return
                    buf = b""

        threading.Thread(target=reader, daemon=True, name="alice-live-in").start()
        try:
            # If the first sentence goes as audio, what was said is unknown: a frame is sent in "auto" too.
            frame = self.grab() if self.wants_frame(first_text, unknown=bool(first_audio)) else None
            if frame and first_audio:
                ws.send(json.dumps({"realtimeInput": {"video": {"data": frame, "mimeType": "image/jpeg"}}}))
            if first_audio:  # the "Hey Alice ..." sentence itself: straight to the model without transcribing
                for i in range(0, len(first_audio), 32000):
                    ws.send(json.dumps({"realtimeInput": {"audio": {
                        "data": base64.b64encode(first_audio[i:i + 32000]).decode(),
                        "mimeType": "audio/pcm;rate=16000"}}}))
                # Then a short silence: so the server sees "speech ended" even if mic frames are late.
                ws.send(json.dumps({"realtimeInput": {"audio": {
                    "data": base64.b64encode(b"\x00\x00" * 9600).decode(), "mimeType": "audio/pcm;rate=16000"}}}))
            if first_text:
                recall = self._recall(first_text)
                parts: list[dict[str, Any]] = [{"text": first_text + ("\n\n" + recall if recall else "")}]
                if frame:
                    parts.insert(0, {"inlineData": {"mimeType": "image/jpeg", "data": frame}})
                ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": parts}],
                                                      "turnComplete": True}}))
                state["heard"].append(first_text + " ")  # so the next transcript isn't glued on ("Navion.Añadían")
                ui.last_user = first_text
            # The mic stream starts AFTER the first sentence is sent: what built up while connecting must not go before
            # or into the first sentence and reach the model out of order.
            threading.Thread(target=sender, daemon=True, name="alice-live-mic").start()
            state["asked"], state["spoke"] = time.monotonic(), False
            self._new_turn()
            self._turn["user"] = first_text
            while not done.is_set() and not stop.is_set():
                try:
                    fc = calls.get(timeout=0.3)
                except queue.Empty:
                    fc = None
                if fc is not None:
                    state["hud"], state["hud_detail"] = "work", HUD_TOOLS.get(fc.get("name"), "")
                    hud()
                    if fc.get("name") == "think":
                        self._fill(state, out, fc)  # thinking always takes long: right away
                    self._run_call(ws, fc)  # for other tools a filler only if not done in 1.5 s (_late_filler)
                    state["tool_done_at"] = time.monotonic()  # the model will see the result and continue: work is ongoing
                    touch()
                    continue
                if state["hud"] == "speak" and not out.playing:
                    state["hud"], state["hud_detail"] = "listen", ""
                hud()
                if out.playing or self._bg:  # _bg: a running think (a background screen task does not keep the session open)
                    touch()
                elif state["pending"] and time.monotonic() - state.get("asked", 0) < self.ANSWER_WAIT:
                    touch()  # the user spoke and an answer is expected: don't close while the model thinks
                working = state.get("tool_done_at", -1e9) >= state.get("turn_done_at", -1e9)
                if working and time.monotonic() - state.get("tool_done_at", -1e9) < self.ANSWER_WAIT:
                    touch()  # the tool result was sent and the model is thinking about the next step: don't close before the work is done
                idle = self.idle
                if self.single_turn and state.get("answered") and not state["pending"] and not working:
                    # selective mode: the answer is done, no follow-up conversation. After a tool result it doesn't
                    # close before the model's turn ends: "kurulumu yap" (do the install) stopped halfway after the
                    # first click (2026-11-02).
                    idle = min(idle, 1.0)
                if time.monotonic() - state["active"] > idle:
                    break
        finally:
            done.set()
            self._ws = self._out = self._late_filler = None
            if ui.asked_at is not None:
                self._ask = (ui.asked_at, ui.since_ask)
            self.last_active = state["active"]
            self._check_claim(ws, state, send=False)
            self._flush_log(state, ui)
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            deadline = time.monotonic() + 15
            while out.playing and time.monotonic() < deadline:  # don't cut off the last sentence
                time.sleep(0.1)
            out.close()
            self.agent.ui = old_ui
            if isinstance(task_state, dict) and (task_state.get("task_hooks") or {}).get("say") is say:
                task_state.pop("task_hooks", None)
            if getattr(out, "dead", None) is not None:
                self.log(f"live ses çıkışı açılamadı: {out.dead}")
            state["hud"] = None
            hud()
            self.log("live kapandı")

    HOLD_BYTES = 24000 * 2 * 3 // 2  # selective mode: buffer max ~1.5 s while the "Alice" transcript is awaited

    @staticmethod
    def _wake_verdict(text: str) -> bool | None:
        """Does Gemini's transcript contain "Alice" (or a spelling of it)? None if the transcript is empty (unknown)."""
        import difflib

        from alice.voice.listen import _NOT_NAMES, _TR, _name_like, strip_wake

        if not text.strip():
            return None
        if strip_wake(text) is not None:
            return True
        words = re.findall(r"\w+", text)
        if any(_name_like(w, True) or w.lower().translate(_TR) == "ali" for w in words):
            return True
        # Gemini may write a lone "Alice" as "Annie" or "Alex". This is only accepted in a very short transcript,
        # with a short, similar-sounding word; "Alışacak, alışacak" or similar words in long sentences don't count.
        first = words[0].lower().translate(_TR) if words else ""
        return (len(words) <= 2 and 4 <= len(first) <= 6 and first not in _NOT_NAMES
                and difflib.SequenceMatcher(None, first, "alice").ratio() >= 0.6)

    # Phrases saying a task was done. Results like "Yok/bulunamadı" (not found) excluded: the tool reports them.
    _DONE_RX = re.compile(
        r"\b(yapt[ıi]m|g[öo]nderdim|ba[şs]latt[ıi]m|indirdim|kurdum|a[çc]t[ıi]m|kapatt[ıi]m|de[ğg]i[şs]tirdim|"
        r"ekledim|kaydettim|ayarlad[ıi]m|not (ettim|ald[ıi]m)|yazd[ıi]m|sildim|tamamlad[ıi]m|kayd[ıi]rd[ıi]m|"
        r"g[öo]nderildi|kuruldu|indirildi|tamamland[ıi])\b", re.I)

    def _check_claim(self, ws, state: dict[str, Any], send: bool = True) -> None:
        """The answer claims a task was done, but no tool that did the work succeeded in this request:
        a correction note goes to the model, which corrects the user. For "WhatsApp'a gönderdim" the window
        was not found; for "tema rengini değiştirdim" no tool had been called. These fabrications also went
        into memory as "onaylandı" (confirmed); the log marks them [doğrulanmadı]. Voice form of the evidence check."""
        said = "".join(state["said"])
        m = self._DONE_RX.search(said)
        turn = getattr(self, "_turn", None) or {}
        if not m or turn.get("ok") or ("claim_fixed" in state and state["claim_fixed"] == state.get("asked")):
            return
        state["claim_fixed"] = state.get("asked")
        state["unverified"] = True
        errors = turn.get("errors") or []
        why = (f"araçlar hata verdi ({'; '.join(errors)[:300]})" if errors else "hiçbir araç çağrılmadı"
               if not turn.get("calls") else "işi yapan araç başarılı sonuç dönmedi")
        self.log(f"live doğrulanmamış iddia ({why}): {said[-160:]}")
        if not send:
            return
        note = (f"[sistem notu, kullanıcının sözü değil] Az önce '{m.group(0)}' dedin ama bu istekte {why}. "
                "Kullanıcıya tek kısa cümleyle bunun yapılmadığını ya da doğrulanamadığını söyle; özür dilemeyi "
                "uzatma, yapıldı deme. İstersen doğru araçla yeniden dene.")
        state["pending"], state["asked"] = True, time.monotonic()  # keep session open until correction is spoken
        state["claim_fixed"] = state["asked"]
        try:
            ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": note}]}],
                                                  "turnComplete": True}}))
        except Exception:  # noqa: BLE001 - connection already closed
            state["pending"] = False

    def _is_read(self, fc: dict[str, Any]) -> bool:
        """Is this a read-only tool (one that changes nothing)?"""
        name, args = fc.get("name", ""), fc.get("args") or {}
        if name == "think" or read_only(name, args):
            return True
        tool = (getattr(self.agent, "tools", None) or {}).get(name) if isinstance(getattr(self.agent, "tools", None),
                                                                                    dict) else None
        try:
            return tool is not None and tool.risk_for(args) == "read"
        except Exception:  # noqa: BLE001
            return False

    def _wrong_target(self, fc: dict[str, Any]) -> str | None:
        """Warning sent back to the model when a typing action's foreground window isn't the app the user named.
        Real case: "WhatsApp'a merhaba yaz" (type hello on WhatsApp) typed "Merhaba" in Claude's window, then Enter."""
        args = fc.get("args") or {}
        if not args.get("text") or str(args.get("action", "")) not in ("type", "set_text", "send", "write"):
            return None
        user = str((getattr(self, "_turn", None) or {}).get("user") or "").lower()
        try:
            names = [n for n in self.names() if len(n) >= 3 and n.lower() in user]
        except Exception:  # noqa: BLE001
            return None
        if not names:
            return None
        from alice.tools import outcome

        if not outcome.IS_WIN:
            return None
        try:
            title = str(outcome.windows().get(outcome.foreground(), {}).get("title") or "")
        except Exception:  # noqa: BLE001
            return None
        if not title or any(n.lower() in title.lower() for n in names):
            return None
        return (f"YAZILMADI: öndeki pencere '{title[:80]}', kullanıcı ise {', '.join(names)} dedi. Önce doğru "
                "pencereye geç (windows action=switch_tab/focus), sonra tekrar dene; emin değilsen kullanıcıya sor.")

    TOOL_TIMEOUT = 25.0  # sec: if a tool hangs, return an error to the model so the conversation doesn't freeze
    BACKGROUND = {"computer_task", "run_plan"}  # can take minutes: runs in background, result goes to chat when done
    # No filler for these: background jobs already say "başladım" (started); the rest are silent bookkeeping.
    NO_FILLER = BACKGROUND | {"task_control", "feedback", "memory"}

    def _latency(self, wait: float, state: dict[str, Any], ui) -> None:
        """Record the turn's latency (weekly summary). If a filler played, the user heard a sound meanwhile."""
        from alice.voice import latency

        e2e = None
        ended = state.pop("ended", None)
        if ended:
            # From the user's last word to the first sound: wake-up, transcript, connection and pre-buffer included.
            # The old measure excluded these (its median of 0.9 s was shorter than what the user actually heard).
            e2e = time.monotonic() - ended + getattr(AudioOut, "PREBUFFER", 0.0)
            self.log(f"live uçtan uca gecikme: {e2e:.1f} sn")
        try:
            latency.record("canlı", wait, model=getattr(self, "model", ""), tools=bool(getattr(ui, "tools", None)),
                           filler=bool(state.get("filled")) and state.get("filled") == state.get("asked"),
                           e2e=round(e2e, 2) if e2e is not None else None)
        except Exception:  # noqa: BLE001 - measurement must not break the conversation
            pass

    def _note_cut(self, ws, state: dict[str, Any], out) -> None:
        """Speech was cut: the played part was heard, the rest was not. The server counts everything it sent
        as spoken (far more than was played); give the model the unheard part as a note."""
        recv, said = state.get("recv", 0), "".join(state["said"])
        if not getattr(self, "cut_notes", True) or not recv or not said or state["mute"]:
            return
        from alice.voice.bargein import cut_note, split_heard

        heard, unheard = split_heard(said, (getattr(out, "played", 0) - state.get("play0", 0)) / recv)
        note = cut_note(heard, unheard)
        if not note:
            return
        try:  # no turnComplete: the note is added to history and the reply goes to the user's new words
            ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": note}]}],
                                                  "turnComplete": False}}))
        except Exception:  # noqa: BLE001
            return
        state["said"] = [(heard + " …(sözü kesildi)").strip()]  # the heard part also goes into the record
        self.log(f"live söz kesildi: cevabın {len(heard)}/{len(said)} karakteri duyuldu")

    def fillers(self):
        """Ready-made phrases in Live's current voice (a new set if the voice changes in Ayarlar)."""
        voice = self.current_voice()
        if self._fillers is None or self._fillers.voice != voice:
            from alice.voice.fillers import gemini_fillers

            self._fillers = gemini_fillers(self.cfg, voice, self.key)
        return self._fillers

    FILL_AFTER = 1.5  # sec: play a filler if the tool isn't done by then (no empty "bir saniye" on quick tools)
    FILL_QUIET = 6.0  # sec: after this much silence in a long tool chain, a second filler

    def _schedule_fill(self, state: dict[str, Any], out, fc: dict[str, Any]):
        """Play a filler if the tool isn't done within FILL_AFTER sec; the returned function cancels it."""
        t = threading.Timer(self.FILL_AFTER, lambda: self._fill(state, out, fc))
        t.daemon = True
        t.start()
        return t.cancel

    def _fill(self, state: dict[str, Any], out, fc: dict[str, Any]) -> None:
        """If the model called a tool this turn before speaking, play a short "bir saniye". If the tool chain runs
        long and nothing was heard for FILL_QUIET sec, play one more (some requests once waited 8-16 sec in silence)."""
        name = fc.get("name", "")
        last = max(state.get("sound_at", 0.0), state.get("filled_at", 0.0))
        quiet = bool(last) and time.monotonic() - last > self.FILL_QUIET
        first = not state.get("spoke") and state.get("filled") != state.get("asked")
        if (not getattr(self, "fillers_on", False) or name in self.NO_FILLER or out.playing
                or not (first or (quiet and state.get("fills", 0) < 2))):
            return
        from alice.voice.fillers import user_tone

        try:
            pcm = self.fillers().get("think" if name == "think" else "tool", user_tone("".join(state.get("heard") or [])[-200:]))
        except Exception:  # noqa: BLE001 - no filler: stay silent, don't break the conversation
            pcm = None
        if pcm:
            state["filled"], state["filled_at"] = state.get("asked"), time.monotonic()
            state["fills"] = state.get("fills", 0) + 1
            out.put(pcm)
            self.log(f"live dolgu çalındı ({name})")

    def _run_background(self, ws, fc: dict[str, Any]) -> None:
        name = fc.get("name", "")
        args = fc.get("args") or {}
        if getattr(self, "_tasks", 0):  # mouse/keyboard are shared: a second screen task would break the first
            try:
                ws.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc.get("id"), "name": name,
                    "response": {"result": "zaten bir ekran görevi sürüyor; aynı işi tekrar başlatma, bitince "
                                           "sonucu sana yazacağım."}}]}}))
            except Exception:  # noqa: BLE001
                pass
            return
        call = {"id": fc.get("id") or name, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}
        try:
            ws.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc.get("id"), "name": name, "response": {
                "result": "başladı, arka planda yapılıyor; bitince sonucu sana yazacağım. Kullanıcıya kısaca "
                          "başladığını söyle, sonucu uydurma."}}]}}))
        except Exception:  # noqa: BLE001
            return
        with self._bg_lock:
            self._tasks = getattr(self, "_tasks", 0) + 1
        turn = getattr(self, "_turn", None)
        if isinstance(turn, dict):
            turn["ok"] = turn.get("ok", 0) + 1  # "başlattım" (I started it) is accurate
        self.log(f"live arka plan işi başladı: {name} {json.dumps(args, ensure_ascii=False)[:160]}")

        def run() -> None:
            try:
                import comtypes  # UI Automation should work on this thread too

                comtypes.CoInitialize()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.agent._enable_groups_for(tool_names=(name,))
                res: ToolResult = self.agent._execute_calls([call])[0]
                text = res.text[:3000]
            except Exception as e:  # noqa: BLE001
                text = f"HATA {type(e).__name__}: {e}"
            self.log(f"live arka plan işi bitti: {name}: {text[:300]}")
            try:
                self._deliver(name, text, ws)
            finally:
                with self._bg_lock:
                    self._tasks = max(0, getattr(self, "_tasks", 1) - 1)

        threading.Thread(target=run, daemon=True, name=f"alice-live-bg-{name}").start()

    def _deliver(self, name: str, text: str, origin=None) -> None:
        """Result of a background job: if a conversation is open (the one that started it, or a later one), it goes
        to the model, which tells the user. If it has closed, announce is used (Ada + voice). A job no longer keeps
        the session open: a closed Discord turn was wasted, and what friends said went to Live; "Alice sus" lost it.
        origin: the connection of the session that started the job (if closed, sending fails and announce is used)."""
        ws = getattr(self, "_ws", None) or origin
        if ws is not None:
            turn = getattr(self, "_turn", None)
            if isinstance(turn, dict) and not text.startswith("HATA"):
                turn["ok"] = turn.get("ok", 0) + 1
            try:
                ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": (
                    f"[{name} bitti, sonucu kullanıcıya kısaca söyle] {text}")}]}], "turnComplete": True}}))
                return
            except Exception:  # noqa: BLE001 - closed at that very moment
                pass
        announce = getattr(self, "announce", None)
        if announce is not None:
            first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0][:160]
            label = "Ekran görevi" if name == "computer_task" else "Görev"
            announce(f"{label} olmadı: {first}" if text.startswith("HATA") else f"{label} bitti: {first}")

    def _run_think(self, ws, fc: dict[str, Any]) -> None:
        """Have the brain model think about it in the background; when the answer arrives, return it to Live as a
        toolResponse. The conversation stays open meanwhile (the user can cut in, "Alice sus" works)."""
        args = fc.get("args") or {}
        question = str(args.get("question") or "").strip()
        lock = threading.Lock()
        answered = [False]

        def respond(text: str) -> None:
            with lock:
                if answered[0]:
                    return
                answered[0] = True
            try:
                ws.send(json.dumps({"toolResponse": {"functionResponses": [
                    {"id": fc.get("id"), "name": "think", "response": {"result": text}}]}}))
            except Exception:  # noqa: BLE001 - conversation already closed
                pass

        if not question:
            respond("HATA: question boş. Kullanıcının sorusunu question'a yaz.")
            return
        if getattr(self, "_thinking", 0):
            respond("Önceki soru hâlâ düşünülüyor; yeni think çağırma. Bu soruya kısaca kendin cevap ver ya da "
                    "kullanıcıya biraz beklemesini söyle.")
            self.log(f"live think atlandı (önceki sürüyor): {question[:120]}")
            return
        self._thinking = getattr(self, "_thinking", 0) + 1
        with self._bg_lock:
            self._bg += 1  # don't let the conversation close while thinking; _think_thread decrements when done
        self.log(f"live think: {question[:200]}")

        released = [False]

        def release() -> None:
            """Thinking must no longer keep the session open: during a 189 sec think, Live stayed open and answered
            Discord conversations. Called once (on timeout or when done)."""
            with lock:
                if released[0]:
                    return
                released[0] = True
            with self._bg_lock:
                self._bg -= 1
            self._thinking = max(0, getattr(self, "_thinking", 1) - 1)

        def too_slow() -> None:
            respond("Düşünmek uzun sürüyor; kullanıcıya biraz daha süreceğini kısaca söyle. Cevap gelince sana ayrıca "
                    "yazacağım, o zaman söyle.")
            self.log("live think zaman aşımı: cevap gelince konuşmaya yazılacak")
            release()

        timer = threading.Timer(self.think_timeout, too_slow)
        timer.daemon = True
        timer.start()
        threading.Thread(target=self._think_thread, args=(ws, question, str(args.get("context") or ""), respond,
                                                          answered, lock, timer, time.monotonic(), release),
                         daemon=True, name="alice-live-think").start()

    def _think_thread(self, ws, question: str, context: str, respond: Callable[[str], None], answered: list[bool],
                      lock: threading.Lock, timer: threading.Timer, t0: float,
                      release: Callable[[], None] | None = None) -> None:
        try:
            try:
                answer = self.think_answer(question, context)
                self.log(f"live think cevabı ({time.monotonic() - t0:.1f} sn): {answer[:200]}")
                text = "Cevap (kendi sesinle, anlamını bozmadan söyle; kısaltabilirsin ama bilgi ekleme): " + answer
            except Exception as e:  # noqa: BLE001
                self.log(f"live think hata: {e}")
                text = f"HATA: düşünülemedi ({type(e).__name__}). Kısa cevabını kendin ver ve emin değilsen söyle."
            timer.cancel()
            with lock:
                late = answered[0]
            if not late:
                respond(text)
                return
            if time.monotonic() - t0 > THINK_STALE:
                # An answer arriving minutes later was read out at an unrelated moment: don't say it.
                self.log(f"live think cevabı çok geç geldi ({time.monotonic() - t0:.0f} sn), söylenmedi")
                return
            try:  # the timeout answer had already gone out: send the result as a new message
                ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": (
                    f"[think bitti, cevabı kullanıcıya söyle] {text}")}]}], "turnComplete": True}}))
            except Exception:  # noqa: BLE001
                pass
        finally:
            if release is not None:
                release()
            else:
                with self._bg_lock:
                    self._bg -= 1
                self._thinking = max(0, getattr(self, "_thinking", 1) - 1)

    def think_answer(self, question: str, context: str = "") -> str:
        """Ask the thinking model: only read-only tools (web, file reading, memory, projects), few steps."""
        from alice.agent import Agent

        parent = self.agent
        tools = [t for name, t in getattr(parent, "tools", {}).items() if name in THINK_TOOLS]
        child = Agent(parent.config, parent.router, tools, parent.ctx, parent.permissions, parent.ui,
                      getattr(parent, "decider", None), system_prompt=parent.messages[0]["content"], is_child=True)
        child.tool_specs = lambda: [t.spec() for t in tools]  # no group filter: all are read-only tools
        child.role = self._think_role(parent.router, question)
        history = self.history_text()
        prompt = THINK_PROMPT.format(history=("\nAz önceki konuşma:\n" + history + "\n") if history else "",
                                     question=question, context=f"\nEk bağlam: {context}" if context else "")
        answer = (child.run_turn(prompt, max_steps=self.think_steps) or "").strip()
        return answer or "bir cevap bulamadım"

    THINK_ROLE = "live-think"

    def _think_role(self, router, question: str = "") -> str:
        """Model order for think: first the fast models measured for voice, then the brain chain. When the model
        at the head of the brain chain hangs (providers.log Oct 25: nvidia:z-ai/glm-5.3 ReadTimeout), every think
        waited out the 180 s timeout: the median of 13 thinks was 189 s, and none finished within 25 s."""
        try:
            refs = list(getattr(self, "think_models", None) or [])
            if not refs:
                refs = router.auto_chain("voice")[:3] + router.chain("brain")
        except Exception:  # noqa: BLE001 - router measurement/role info unavailable: old behavior
            return "brain"
        chain = [r for i, r in enumerate(refs) if r not in refs[:i]]
        if not chain:
            return "brain"
        if question:  # Jev routing: heavy jobs -> [routing] voice_strong (e.g. Sonnet 5.5), the rest -> fast chain
            try:
                from alice.providers import jev_route

                chain, r = jev_route.voice_chain(router, self.cfg, question, chain, log=self.log)
                self.log(f"live think yönlendirme: {r.tier} ({r.reason}) -> {chain[0]}")
            except Exception as e:  # noqa: BLE001
                self.log(f"live think yönlendirme hatası: {e}")
        router.role_overrides[self.THINK_ROLE] = chain  # as in council.py: temp role, own chain
        return self.THINK_ROLE

    def _execute(self, call: dict[str, Any]) -> ToolResult | None:
        """Run the tool on the tool thread. If it hangs (e.g. while reading a window's elements), abandon that
        thread so the next tools run on a new one: the conversation must not lock up. None = hung."""
        if getattr(self, "_tools_q", None) is None:
            q: queue.Queue = queue.Queue()
            self._tools_q = q
            threading.Thread(target=self._tool_worker, args=(q,), daemon=True, name="alice-live-tools").start()
        job: dict[str, Any] = {"call": call, "done": threading.Event()}
        self._tools_q.put(job)
        if not job["done"].wait(self.TOOL_TIMEOUT + 1):
            self.log(f"live araç takıldı, araçlar yeni iş parçacığına geçti: {call['function']['name']}")
            self._tools_q = None
            state = getattr(getattr(self.agent, "ctx", None), "state", None)
            if isinstance(state, dict):
                state.pop("browser", None)  # the browser connection is bound to the old thread: let it reconnect
            return None
        return job["res"]

    def _on_tool_thread(self, fn: Callable[[], ToolResult], timeout: float = 600.0) -> ToolResult:
        """Run a background task's tool on the conversation's tool thread (the browser connection lives there)."""
        q = getattr(self, "_tools_q", None)
        if q is None:
            q = queue.Queue()
            self._tools_q = q
            threading.Thread(target=self._tool_worker, args=(q,), daemon=True, name="alice-live-tools").start()
        job: dict[str, Any] = {"fn": fn, "done": threading.Event()}
        q.put(job)
        if not job["done"].wait(timeout):
            return ToolResult(f"araç {timeout:.0f} sn içinde bitmedi", is_error=True)
        return job["res"]

    def _tool_worker(self, q: "queue.Queue[dict[str, Any]]") -> None:
        try:
            import comtypes  # so UI Automation also works on this thread

            comtypes.CoInitialize()
        except Exception:  # noqa: BLE001
            pass
        while True:
            job = q.get()
            call = job.get("call")
            try:
                if "fn" in job:  # a background task's tool: browser/UIA objects are bound to this thread
                    job["res"] = job["fn"]()
                else:
                    self.agent._enable_groups_for(tool_names=(call["function"]["name"],))
                    job["res"] = self.agent._execute_calls([call])[0]
            except Exception as e:  # noqa: BLE001
                job["res"] = ToolResult(f"{type(e).__name__}: {e}", is_error=True)
            job["done"].set()

    def _new_turn(self) -> None:
        """The user said something new: tool counter, repeat check and external-content contamination are reset."""
        self._turn = {"n": 0, "last": None, "seen": {}, "ok": 0, "errors": [], "calls": 0, "fails": {}}
        from alice import guard

        guard.clear(getattr(self.agent, "ctx", None))

    def _guard(self, fc: dict[str, Any]) -> str | None:
        """The reply to return to the model instead of running the tool (None = run it). Two cases:
        same call back to back (e.g. checklist_next twice, steps got skipped): the previous result is returned;
        if the per-request tool count is exceeded, the model is stopped and told to return to the user."""
        turn = getattr(self, "_turn", None)
        if turn is None:
            turn = self._turn = {"n": 0, "last": None}
        name, args = fc.get("name", ""), fc.get("args") or {}
        key = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
        last = turn.get("last")
        repeatable = any(w in str(args.get("action", "")) for w in ("scroll", "key", "wait", "press", "screenshot"))
        if last and last[0] == key and time.monotonic() - last[1] < 5 and not repeatable:
            self.log(f"live aynı araç çağrısı tekrarlandı, çalıştırılmadı: {name}")
            return ("Bu çağrıyı az önce aynen yaptın, tekrar çalıştırılmadı. Önceki sonuç: " + last[2][:1500]
                    + "\nAynı aracı tekrar çağırma; bu sonuca göre kullanıcıya cevap ver.")
        # Even with other calls in between, the same read must not run twice in one request (voice.log:
        # Get-Volume, Downloads listing 5-6 times a minute). State-changing actions (click, type) skip this check.
        seen = turn.setdefault("seen", {})
        if key in seen and not repeatable and read_only(name, args):
            self.log(f"live aynı okuma bu istekte tekrarlandı, çalıştırılmadı: {name}")
            return ("Bu okumayı bu istekte zaten yaptın, sonuç değişmedi: " + seen[key][:1500]
                    + "\nBaşka bir yol dene ya da bulduğunu kullanıcıya söyle.")
        turn["n"] += 1
        budget = int(getattr(self, "tool_budget", 10))
        if turn["n"] > budget:
            if turn["n"] == budget + 1:
                self.log(f"live araç sınırı: bu istekte {budget} araç çağrıldı, model kullanıcıya dönmeye zorlandı")
            return (f"DUR: bu istek için {budget} araç çağrısı sınırına geldin, araç ÇALIŞTIRILMADI. Daha fazla araç "
                    "çağırma. Kullanıcıya şimdi tek iki cümleyle neyi bulduğunu, neyin olmadığını söyle ve devam "
                    "etmemi ister misin diye sor.")
        return None

    def _remember(self, fc: dict[str, Any], text: str) -> None:
        turn = getattr(self, "_turn", None)
        if turn is not None:
            key = fc.get("name", "") + json.dumps(fc.get("args") or {}, sort_keys=True, ensure_ascii=False)
            turn["last"] = (key, time.monotonic(), text)
            if read_only(fc.get("name", ""), fc.get("args") or {}):
                turn.setdefault("seen", {})[key] = text

    def _run_call(self, ws, fc: dict[str, Any]) -> None:
        early = self._guard(fc)
        if early is not None:
            try:
                ws.send(json.dumps({"toolResponse": {"functionResponses": [
                    {"id": fc.get("id"), "name": fc.get("name"), "response": {"result": early}}]}}))
            except Exception:  # noqa: BLE001 - connection may already be closed
                pass
            return
        turn = getattr(self, "_turn", None)
        if turn is None:
            turn = self._turn = {"n": 0, "last": None}
        turn["calls"] = turn.get("calls", 0) + 1
        user = str(turn.get("user") or "")
        if not self._is_read(fc) and foreign(user):
            # A misheard sentence (game, video, another language) must not turn into an action: starting Unciv,
            # opening a song, pressing Install all used to happen this way. Reads are allowed.
            self.log(f"live dil kapısı: Türkçe olmayan istekle {fc.get('name')} çalıştırılmadı: {user[:120]}")
            self._reply(ws, fc, "YAPILMADI: duyulan söz Türkçe değil ya da anlaşılmadı, büyük ihtimalle sana "
                                "söylenmedi (oyun/video/başkası). Hiçbir şey söyleme, sessiz kal.")
            return
        wrong = self._wrong_target(fc)
        if wrong:
            self.log(f"live hedef pencere uyuşmadı: {wrong[:160]}")
            self._reply(ws, fc, wrong)
            return
        if fc.get("name") in self.BACKGROUND:
            self._run_background(ws, fc)
            return
        if fc.get("name") == "think":
            self._run_think(ws, fc)
            return
        call = {"id": fc.get("id") or fc.get("name"), "type": "function",
                "function": {"name": fc.get("name", ""), "arguments": json.dumps(fc.get("args") or {})}}
        if fc.get("name") == "task_control":  # immediate: the tool thread may be busy with a step of the task
            try:
                res = self.agent._execute_calls([call])[0]
            except Exception as e:  # noqa: BLE001 - error escaped converse; the old path re-ran the first sentence
                res = ToolResult(f"HATA {type(e).__name__}: {e}", is_error=True)
            self.log(f"live görev kontrolü {json.dumps(fc.get('args') or {}, ensure_ascii=False)}: {res.text[:120]}")
            try:
                ws.send(json.dumps({"toolResponse": {"functionResponses": [
                    {"id": fc.get("id"), "name": "task_control", "response": {"result": res.text}}]}}))
            except Exception:  # noqa: BLE001
                pass
            return
        lock = threading.Lock()
        answered = [False]

        def respond(text: str) -> None:
            with lock:
                if answered[0]:
                    return
                answered[0] = True
            try:
                ws.send(json.dumps({"toolResponse": {"functionResponses": [
                    {"id": fc.get("id"), "name": fc.get("name"), "response": {"result": text}}]}}))
            except Exception:  # noqa: BLE001 - connection may already be closed
                pass

        def too_slow() -> None:
            self.log(f"live araç zaman aşımı: {fc.get('name')} {json.dumps(fc.get('args') or {}, ensure_ascii=False)[:120]}")
            respond(f"HATA: araç {self.TOOL_TIMEOUT:.0f} sn içinde bitmedi, iş YAPILMADI. Kullanıcıya olmadığını "
                    "kısaca söyle (yapıldı deme); başka yoldan dene (ör. computer_task).")

        timer = threading.Timer(self.TOOL_TIMEOUT, too_slow)
        timer.daemon = True
        timer.start()
        late = getattr(self, "_late_filler", None)
        cancel = late(fc) if late is not None else None
        try:
            res = self._execute(call)
        finally:
            if cancel is not None:
                cancel()
        if res is None:  # hung: the timeout reply was sent, the conversation continues with the next request
            return
        text = res.text[:6000]
        name = fc.get("name", "")
        # Every result goes to the log: it used to record only 4 tools (37 results for 361 calls), so replies
        # based on flight/window/command results could not be verified.
        self.log(f"live araç sonucu {name} [{fc.get('id') or '-'}] {'hata' if res.is_error else 'tamam'}: "
                 f"{text[:200].replace(chr(10), ' | ')}")
        fails = turn.setdefault("fails", {})
        if res.is_error and "reddetti" in text:
            text = "onay gerekli: kullanıcıya kısaca sor, 'evet' derse aynı çağrıyı tekrarla."
        elif res.is_error:
            turn.setdefault("errors", []).append(f"{name}: {text[:120]}")
            fails[name] = fails.get(name, 0) + 1
            text = "HATA: " + text
            if fails[name] >= 2:
                # The model kept repeating the same tool, then asked the user to "tarayıcı ayarlarını kontrol et".
                text += ("\nBu araç bu istekte üst üste başarısız oldu: aynısını tekrar deneme. Başka bir araçla dene "
                         "ya da kullanıcıya olmadığını kısaca söyle. Kullanıcıdan ayar değiştirmesini isteme.")
        else:
            fails[name] = 0
            turn["ok"] = turn.get("ok", 0) + 1
        timer.cancel()
        self._remember(fc, text)
        for img in (getattr(res, "images", None) or [])[:1]:  # tool image (screen/tab): show it to the model
            frame = _to_jpeg(img)
            if frame:
                try:
                    ws.send(json.dumps({"realtimeInput": {"video": {"data": frame, "mimeType": "image/jpeg"}}}))
                    text += "\n(görüntü sana ayrıca gönderildi)"
                except Exception:  # noqa: BLE001
                    pass
        respond(text)

    def _reply(self, ws, fc: dict[str, Any], text: str) -> None:
        try:
            ws.send(json.dumps({"toolResponse": {"functionResponses": [
                {"id": fc.get("id"), "name": fc.get("name"), "response": {"result": text}}]}}))
        except Exception:  # noqa: BLE001 - connection may already be closed
            pass

    def _record_correction(self, prev: tuple[float, str, str], heard: str) -> None:
        """The user corrected the previous turn ("sana demedim" = "I didn't say that", "yanlış anladın" =
        "you misunderstood"): that turn goes into the test set and the feedback log; the nightly loop starts from
        these. On the voice side this signal was never evaluated."""
        ts, user, reply = prev
        if time.time() - ts > 120:
            return  # too old: not about this turn
        self._corrected(heard)
        try:
            from alice.evals import record_feedback

            record_feedback(user, reply, note=f"kullanıcı hemen ardından: {heard[:200]}", source="ses-düzeltme")
            self.log(f"live düzeltme test setine eklendi: {user[:80]!r} -> {heard[:80]!r}")
        except Exception:  # noqa: BLE001 - a recording error must not break the conversation
            pass

    def _corrected(self, text: str) -> None:
        hook = getattr(self, "on_correction", None)
        if hook is not None:
            try:
                hook(text)
            except Exception:  # noqa: BLE001 - a corpus error must not break the conversation
                pass

    def _memory(self):
        mem = getattr(self.agent, "memory", None)
        return mem if mem is not None and hasattr(mem, "recall") else None

    def _recall(self, text: str) -> str:
        mem = self._memory()
        try:
            return mem.recall(text, use_model=False) if mem is not None else ""  # voice: no added latency
        except Exception:  # noqa: BLE001
            return ""

    def _flush_log(self, state: dict[str, Any], ui: LiveUI) -> None:
        heard, said = "".join(state["heard"]).strip(), "".join(state["said"]).strip()
        if state.pop("unverified", False) and said:
            # "yaptım" (done) without a tool result: memory/summary must not record it as a fact
            # ("kullanıcı onayladı" = "user confirmed").
            said = "[doğrulanmadı] " + said
        if heard or said or ui.tools:
            tools = f" araçlar: {', '.join(ui.tools)}" if ui.tools else ""
            self.log(f"live [{self.model}] duyulan: {heard} | cevap: {said}{tools}")
        if state.pop("cut", False) and not heard:
            self.log("live boş kesme: söz kesildi ama kullanıcıdan söz duyulmadı (yankı ya da gürültü)")
        history = getattr(self, "_history", [])
        if heard and history and correction(heard):
            self._record_correction(history[-1], heard)
        if heard:
            self._history = (history + [(time.time(), heard, said)])[-20:]
        elif said and history and state.get("ep_open"):
            # A reply arriving in a separate turn after a tool belongs to the user's question
            # (otherwise the logged reply stayed empty).
            ts, user, before = history[-1]
            self._history[-1] = (ts, user, (before + " " + said).strip())
        mem = self._memory()
        if mem is not None and (heard or (said and state.get("ep"))):
            try:
                if heard:
                    state["ep"] = mem.record(heard, said, "canlı")
                    mem.learn(heard, said)
                elif state.get("ep_open"):
                    mem.add_reply(state["ep"], said)
            except Exception:  # noqa: BLE001 - a memory error must not break the conversation
                pass
        if heard:
            state["ep_open"] = True  # a later reply without user input is added to this turn
        state["heard"], state["said"] = [], []
        ui.tools = []
