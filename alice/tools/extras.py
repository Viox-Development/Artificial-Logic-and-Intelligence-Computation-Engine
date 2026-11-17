"""Tools for the extras: notes, morning brief, weekly report, media and shutdown timer."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from alice.tools.base import COMPUTER, DANGER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class Notes(Tool):
    name = "notes"
    description = ("Kullanıcının kısa notlarını kaydeder, arar ve listeler ('not al', 'şunu not et', 'notlarımda ne vardı').\n"
                   "- Kullanıcının söylediğini olduğu gibi kaydet; özetleme ya da yorum ekleme.\n"
                   "- Kullanıcı hakkında kalıcı bilgi ve tercihler için memory, uçuş sırasındaki notlar için flight "
                   "kneeboard_note kullan.\n"
                   "- undo sadece son eklenen notu siler.")
    parameters = obj({
        "action": {"type": "string", "enum": ["add", "search", "list", "undo"],
                   "description": "add: text'i kaydet | search: query ve/veya tarih aralığıyla ara | list: son 20 not "
                   "| undo: son eklenen notu sil."},
        "text": {"type": "string", "description": "add için notun metni, kullanıcının söylediği gibi."},
        "query": {"type": "string", "description": "search için aranacak kelimeler."},
        "since": {"type": "string", "description": "Başlangıç: '7d', '2h', 'dün', 'bugün' ya da ISO tarih."},
        "until": {"type": "string", "description": "Bitiş, since ile aynı biçim. Boş: şimdi."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ

    def run(self, ctx: ToolContext, action: str, text: str = "", query: str = "", since: str | None = None,
            until: str | None = None) -> ToolResult:
        from alice.core.store import parse_time
        from alice.extras import notes

        if action == "add":
            if not text.strip():
                raise ToolError("text gerekli")
            notes.add(text)
            return ToolResult("not alındı")
        if action == "undo":
            n = notes.remove_last()
            return ToolResult(f"silindi: {n.text}" if n else "silinecek not yok")
        try:
            t0, t1 = parse_time(since), parse_time(until)
        except ValueError as e:
            raise ToolError(str(e)) from e
        found = notes.search(query if action == "search" else "", t0, t1)
        if action == "list":
            found = found[-20:]
        return ToolResult("\n".join(n.line() for n in found[-50:]) or "not bulunamadı")


class Report(Tool):
    name = "report"
    description = ("Hazır özet raporların verisini döndürür.\n"
                   "- morning: sabah özeti (hava, bugün okul/staj, gece çalışan görevler ve uyarılar, dünden kalanlar). "
                   "Kullanıcıya kısa ve sıcak bir dille anlat; listeyi aynen okuma.\n"
                   "- pc: son günlerin bilgisayar raporu (sıcaklık, kullanım, olaylar) ve bir HTML dosyası; dosyayı send_file ile "
                   "gösterebilirsin.")
    parameters = obj({
        "kind": {"type": "string", "enum": ["morning", "pc"], "description": "morning: sabah özeti | pc: PC raporu."},
        "days": {"type": "integer", "description": "pc için kaç günlük. Varsayılan 7."},
    }, ["kind"])
    risk = READ

    def run(self, ctx: ToolContext, kind: str, days: int = 7) -> ToolResult:
        from alice.core.schedule import TaskStore
        from alice.extras import brief, report
        from alice.tools.core import _store

        if kind == "morning":
            return ToolResult(brief.render(brief.gather(ctx.config.data.get("brief", {}), _store(ctx), TaskStore())))
        r = report.build(_store(ctx), days)
        return ToolResult(f"{report.text(r)}\nRapor: {report.save_html(r)}")


class Weather(Tool):
    name = "weather"
    description = ("Bir şehrin şu anki ve bugünkü hava durumunu döndürür: sıcaklık, en düşük/yüksek ve yağmur ihtimali.\n"
                   "- Hava sorulunca web_search ya da web_fetch yerine bunu kullan; daha hızlı ve kesin.\n"
                   "- Şehir verilmezse ayarlardaki şehir ya da konum kullanılır.\n"
                   "- Sadece bugünü kapsar; ileriki günler sorulursa bunu söyle, tahmin uydurma.")
    parameters = obj({"city": {"type": "string", "description": "Şehir, ör. 'İzmir'. Boş: ayarlı şehir ya da konum."}})
    risk = READ

    def run(self, ctx: ToolContext, city: str = "") -> ToolResult:
        from alice.extras import brief

        return ToolResult(brief.weather(city or ctx.config.data.get("brief", {}).get("city", ""), auto=True))


class Media(Tool):
    name = "media"
    description = ("Çalan medyayı (müzik, video) kontrol eder ve ne çaldığını söyler (now): Spotify, Sonora, YouTube, "
                   "hangi uygulama olursa. \"Ne çalıyor\" sorusunda önce bunu kullan (Spotify bağlantısı yalnız Spotify'ı "
                   "görür). Sistem sesi, zamanlı durdurma, bilgisayarı kapatma. Yeni bir şarkı ya da video AÇMAK için "
                   "değil (youtube ya da browser).")
    parameters = obj({
        "action": {"type": "string", "enum": ["now", "play_pause", "next", "previous", "stop", "mute", "volume_up",
                                              "volume_down", "set_volume", "pause_in", "shutdown_in",
                                              "cancel_shutdown"],
                   "description": "now: ne çalıyor (şarkı, sanatçı, uygulama) | play_pause: durdur/devam | next/previous: sonraki/önceki parça | mute: sesi kapat/aç "
                   "| volume_up/volume_down: times kadar adım | set_volume: percent'e ayarla | pause_in: minutes "
                   "sonra müziği durdur | shutdown_in: minutes sonra bilgisayarı kapat | cancel_shutdown."},
        "times": {"type": "integer", "description": "volume_up/down için basış sayısı (her biri ~%2). Varsayılan 5."},
        "percent": {"type": "integer", "description": "set_volume için 0-100."},
        "minutes": {"type": "number", "description": "pause_in ve shutdown_in için dakika."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return DANGER if args.get("action") == "shutdown_in" else COMPUTER

    def run(self, ctx: ToolContext, action: str, times: int = 5, percent: int | None = None,
            minutes: float | None = None) -> ToolResult:
        from alice.extras import media

        if action == "set_volume":
            if percent is None:
                raise ToolError("percent gerekli")
            return ToolResult(media.set_volume(percent))
        if action in ("pause_in", "shutdown_in"):
            if not minutes or minutes <= 0:
                raise ToolError("minutes gerekli")
            return ToolResult(media.pause_in(minutes) if action == "pause_in" else media.shutdown_in(minutes))
        if action == "cancel_shutdown":
            return ToolResult(media.cancel_shutdown())
        from alice.extras import nowplaying

        if action == "now":
            return ToolResult(nowplaying.describe(nowplaying.now()))
        if action in nowplaying.CONTROL and nowplaying.control(action):
            # Goes to the app actually playing (the media key used to hit the last-focused one); report, don't assume
            import time

            time.sleep(0.7)
            return ToolResult(f"tamam; şimdi: {nowplaying.describe(nowplaying.now())}")
        return ToolResult(media.key(action, times if action.startswith("volume") else 1))


YT_VIDEO = re.compile(r'"videoRenderer":\{"videoId":"([\w-]{11})".*?"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', re.S)


YT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/130.0 Safari/537.36", "Accept-Language": "tr-TR,tr;q=0.9"}


_YT_CACHE: dict[str, tuple[float, list[tuple[str, str]]]] = {}


def youtube_search(query: str, limit: int = 5, client: httpx.Client | None = None) -> list[tuple[str, str]]:
    """YouTube search: [(title, video_id)]. Read from the ytInitialData in the page HTML (no API key needed).
    Results are cached for 10 min: YouTube may reorder results on every request, so the "first video" stays the same."""
    import time

    key = query.strip().lower()
    hit = _YT_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < 600 and client is None:
        return hit[1][:limit]
    c = client or httpx.Client(timeout=8, follow_redirects=True, cookies={"CONSENT": "YES+1", "SOCS": "CAI"})
    try:
        r = c.get("https://www.youtube.com/results", params={"search_query": query, "hl": "tr", "gl": "TR"},
                  headers=YT_HEADERS)
    finally:
        if client is None:  # new client for every search: don't leave the socket open
            c.close()
    out: list[tuple[str, str]] = []
    for vid, title in YT_VIDEO.findall(r.text):
        if vid not in (v for _, v in out):
            try:
                title = json.loads(f'"{title}"')
            except ValueError:
                pass
            out.append((title, vid))
        if len(out) >= limit:
            break
    if out:
        _YT_CACHE[key] = (time.monotonic(), out)
    return out


def with_time(url: str, seconds: int) -> str:
    """Add or replace the start time in seconds on a YouTube URL (&t=120)."""
    url = re.sub(r"([?&])t=\d+s?&?", r"\1", url).rstrip("?&")
    return f"{url}{'&' if '?' in url else '?'}t={max(0, int(seconds))}"


class YouTube(Tool):
    name = "youtube"
    description = ("YouTube'da yeni bir video bulup kullanıcının kendi tarayıcısında açar (açık YouTube sekmesi varsa "
                   "onda) ya da açık videoyu bir dakikaya sarar.\n"
                   "- Kullan: ekranda olmayan bir videoyu açmak ('X videosunu aç', 'Y şarkısını çal'), 'ikinci "
                   "dakikaya getir'.\n"
                   "- Kullanma: ekranda zaten görünen bir video/şarkı (browser snapshot + click ya da reflex), bir "
                   "siteyi açmak (YouTube Music dahil: windows navigate ya da browser goto), durdur/devam (media "
                   "play_pause).\n"
                   "- query'yi kullanıcının söylediği gibi yaz; ne istediği belirsizse önce sor.")
    parameters = obj({
        "action": {"type": "string", "enum": ["search", "play", "seek"],
                   "description": "search: query için ilk videoları listeler (bir şey açmaz) | play: query'nin "
                   "index'inci videosunu ya da url'yi açar | seek: açık videoyu at saniyesine götürür."},
        "query": {"type": "string", "description": "Arama ifadesi, ör. 'Coldplay Yellow'."},
        "url": {"type": "string", "description": "play için doğrudan video adresi (query yerine)."},
        "index": {"type": "integer", "description": "play için kaçıncı sonuç; 1 = ilk. Varsayılan 1."},
        "at": {"type": "integer", "description": "Başlangıç saniyesi; ör. 2. dakika = 120."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") == "search" else COMPUTER

    def run(self, ctx: ToolContext, action: str, query: str | None = None, url: str | None = None,
            index: int = 1, at: int | None = None) -> ToolResult:
        from alice.tools.desktop import current_tab_url, navigate_tab

        if action == "seek":
            if at is None:
                raise ToolError("seek için at (saniye) gerekli")
            cur = url or current_tab_url()
            if "youtube.com/watch" not in cur and "youtu.be/" not in cur:
                raise ToolError(f"öndeki sekmede YouTube videosu yok ({cur[:80] or 'adres okunamadı'}); önce play")
            return ToolResult(navigate_tab(with_time(cur, at), new_tab=False))
        if action == "search" or (action == "play" and not url):
            if not query:
                raise ToolError("query gerekli")
            try:
                hits = youtube_search(query)
            except httpx.HTTPError as e:
                raise ToolError(f"YouTube'a ulaşılamadı: {e.__class__.__name__}") from e
            if not hits:
                return ToolResult("video bulunamadı")
            if action == "search":
                return ToolResult("\n".join(f"{i}. {t} — https://www.youtube.com/watch?v={v}"
                                            for i, (t, v) in enumerate(hits, 1)))
            title, vid = hits[max(1, min(int(index or 1), len(hits))) - 1]
            url = f"https://www.youtube.com/watch?v={vid}"
        else:
            title = url
        if at:
            url = with_time(url, at)
        return ToolResult(f"oynatılıyor: {title}. {navigate_tab(url)}")


TOOLS = [Notes(), Report(), Weather(), Media(), YouTube()]
