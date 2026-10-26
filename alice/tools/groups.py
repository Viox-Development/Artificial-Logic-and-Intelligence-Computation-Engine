"""Araç grupları: her istekte 33 aracın tanımını (~5K token) göndermek yerine sadece gerekenleri gönder.

Temel araçlar her zaman gider. Diğer gruplar kullanıcının mesajındaki anahtar kelimelerle, konuşmada
daha önce kullanılmışlarsa ya da model `load_tools` ile istediğinde eklenir ve o oturum boyunca kalır.
Daha küçük istek = daha fazla ücretsiz modele sığar, daha hızlı ve daha az kafa karıştırıcı.
"""

from __future__ import annotations

import re
from typing import Any

from alice import plugins
from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult, obj

# Her istekte tam tanımıyla giden araçlar: en sık gerekenler. Diğerleri (Claude Code'daki gibi) sadece adlarıyla
# sistem talimatında listelenir; kullanıcının sözüne göre kendiliğinden ya da model load_tools deyince yüklenir.
# Önceden 27 araç her istekte ~9K token tutuyordu: dar bağlamlı/hızlı ücretsiz modeller (Groq, Cerebras) ana model
# olamıyordu.
CORE = {"read_file", "write_file", "edit_file", "list_dir", "glob", "grep", "run_command", "web_fetch", "web_search",
        "memory", "load_tools", "ask_user"}

# grup -> (araçlar, kısa açıklama, anahtar kelime regex'i)
GROUPS: dict[str, tuple[set[str], str, str]] = {
    "files": ({"file_ops", "multi_edit", "apply_patch", "notebook_edit"},
              "dosya taşıma/kopyalama/silme, bir dosyada çok değişiklik, çok dosyaya yama, Jupyter defteri",
              r"taşı|kopyala|\bsil(in|e|er misin)?\b|yeniden adlandır|adını değiştir|çöp|düzenle|değiştir|düzelt|refactor|"
              r"yama|patch|notebook|ipynb|jupyter|\bkod|code|script|fonksiyon|\.py\b|\.js\b|\.ts\b|bug|klasör"),
    "plan": ({"todo_write", "run_plan", "task_control", "task", "process_output", "process_stop", "skill"},
             "çok adımlı iş planı ve yapılacaklar listesi, alt ajan, arka plandaki komutun çıktısı/durdurma, beceriler",
             r"önce .* sonra|ardından|sonra da|adım adım|planla|alt ajan|arka planda|background|sunucu(yu)? başlat|"
             r"dev server|npm run|\bwatch\b|beceri|skill"),
    "desktop": ({"computer", "computer_task", "ui_elements", "ui_act", "clipboard", "reflex", "app", "windows", "game"},
                "uygulama/pencere açma-kapama, ekran görüntüsü, fare/klavye, masaüstü uygulamalarında tıklama, pano, "
                "oyun oturumu (turlar, notlar)",
                r"tıkla|ekran|pencere|fare|mouse|klavye|screenshot|buton|düğme|menü|click|screen|pano|kopyala|"
                r"yapıştır|clipboard|oyun|ayarlar|uygulamada|arayüz|simge|ikon|whatsapp|instagram|insta\b|"
                r"tiktok|dm at|mesaj at|uygulama|\baç|kapat|başlat|küçült|sekme|unciv|sıra tabanlı|strateji oyunu|"
                r"tur(u|unu)? (oyna|bitir|geç)"),
    "browser": ({"browser"}, "kullanıcının tarayıcısında web sayfası okuma, tıklama, form doldurma",
                r"site|tarayıcı|browser|chrome|edge|firefox|opera|google|sayfa|link|https?://|www\.|\.com|"
                r"giriş yap|login|online|internetten|instagram|twitter|github|e-?posta|mail"),
    "github": ({"git", "github"}, "git (commit, dal, push) ve GitHub (depo klonlama, PR açma/birleştirme, CI durumu)",
               r"git\b|git ?hub|\brepo|depo|commit|push|pull|pull request|\bpr\b|merge|birleştir|branch|\bdal\b|"
               r"issue|klonla|clone|\bci\b"),
    "system": ({"system_info", "process_list", "process_kill", "metrics"},
               "CPU/RAM/GPU/disk durumu, çalışan işlemler, geçmiş ölçümler",
               r"cpu|ram|gpu|işlemci|ekran kartı|sıcaklık|derece|bellek|disk|pil|batarya|performans|işlem|süreç|"
               r"process|görev yöneticisi|kasıyor|yavaş|fps|metrik"),
    "automation": ({"rules", "schedule", "feeds", "action_log"},
                   "uyarı kuralları, zamanlanmış görevler, servis akışları ve gönderilenlerin kaydı",
                   r"kural|uyar|hatırlat|zamanla|her gün|her sabah|her akşam|haftada|alarm|schedule|rule|cron|"
                   r"saat \d|otomatik|haber ver|söyle bana|gelirse|girince|olunca|bildir|takip et|izle|"
                   r"ne gönderdin|kime yazdın|ne yazdın|benim adıma|gönderdiklerin"),
    "blender": ({"blender"}, "Blender'da 3B sahne (bpy betiği)",
                r"blender|3d|3b|üç boyut|modelle|sahne|render|mesh"),
    "flight": ({"flight"}, "DCS / War Thunder uçuş verisi ve kneeboard",
               r"dcs|war ?thunder|uçuş|uçak|kneeboard|pist|iniş|kalkış|simülasyon|sim\b"),
    "connect": (set(),  # araçlar çalışma anında tanımlardan gelir (alice/connect)
                "bağlı servisler: Spotify, Steam, Discord, YouTube, eklediğin API'lar",
                r"spotify|müzik|şark[ıi]|parça|çal\b|duraklat|steam|oyun kütüphanesi|discord|sunucu|kanal|"
                r"abone|izlenme|playlist|çalma listesi"),
    "net": ({"http_request"}, "REST API çağırma, webhook, JSON gönderme, URL'den dosya indirme",
            r"\bapi\b|endpoint|webhook|\bjson\b|\brest\b|http isteği|post isteği|istek at|indir|download|curl"),
    "team": ({"team", "council"}, "büyük işleri otonom ajan projesine bölme ve inceleme kurulu (uzmanlar "
             "tartışıp oylar)",
             r"takım|ekip|paralel|aynı anda|ajanlar|ajan çağır|orkestra|alt ajan|subagent|multi.?agent|büyük (iş|proje|"
             r"kod)|codebase|kod tabanı|refactor|yeniden düzenle|kurul|eleştir|tartış|savun|artı.*eksi|incele"),
    "personal": ({"notes", "report", "media", "youtube", "weather", "feedback"},
                 "notlar, sabah/PC raporu, müzik, YouTube'da video açma, ses seviyesi, hava durumu, beğenilmeyen cevap",
                 r"\bnot\b|notlar|not al|rapor|özet|sabah|müzik|şarkı|ses seviyesi|sesi aç|sesi kıs|spotify|"
                 r"kapat .*dakika|dakika sonra|youtube|video|klip|izle|\bçal\b|hava|yağmur|kar yağ|derece|sıcak|soğuk|"
                 r"şemsiye|beğenmedim|yanlış (yaptın|cevap)|kötü cevap"),
    "projects": ({"projects"}, "kullanıcının projeleri (repo, Unity, Python...) ve ayrıntıları",
                 r"proje|repo\b|depo(su|m|yu)?\b|unity|godot|kodum|oyunum|oyunumu|uygulamam|scriptim|programım|"
                 r"son commit|nerede duruyor|hangi klasör"),
    "mail": ({"gmail"}, "Gmail (birden fazla hesap): e-posta ara, oku, taslak, gönder",
             r"e-?posta|\bmail|gmail|gelen kutu|inbox|okunmamış|taslak|cevap yaz|yanıtla"),
    "home": ({"pc", "wifi", "android", "tv"}, "PC gücü/parlaklık, Wi-Fi ve ev ağı, TV, Android telefon",
             r"kilitle|uyut|uyku|hazırda|yeniden başlat|restart|parlaklı|monitör|ekranı kapat|güç plan|wi-?fi|"
             r"kablosuz|internet|modem|\bağ(a|da|ı|daki)?\b|ssid|\btv\b|televizyon|android|telefon|bildirim|"
             r"\bpil\b|şarj|wake.?on|uzaktan aç|\bmac\b|netflix|samsung|kanal"),
}

TOOL_TO_GROUP = {name: g for g, (names, _, _) in GROUPS.items() for name in names}

# Büyük gruplarda sözden bütün grubu değil, sadece gereken aracı aç: "YouTube'da lofi aç" için 8 masaüstü aracı ve
# not/rapor araçları değil, youtube yeter. Burada adı geçen grupların araçları sadece kendi kalıplarıyla açılır;
# grubun tamamı model load_tools deyince ya da gruptan bir araç kullanılınca açılır.
TOOL_HINTS: dict[str, str] = {
    "windows": r"\baç|kapat|pencere|uygulama|başlat|küçült|büyüt|sekme|program",
    "computer": r"ekran|screenshot|fare|mouse|klavye|tıkla|sürükle|kaydır|bas\b",
    "reflex": r"reflex|tıkla|ekrana bak|ekranda|butona|düğmeye|menüden",
    "ui_elements": r"tıkla|buton|düğme|menü|arayüz|ayarlar|simge|ikon|uygulamada|pencerede",
    "ui_act": r"tıkla|buton|düğme|menü|arayüz|ayarlar|simge|ikon|uygulamada|pencerede",
    "computer_task": r"ekranda .*(yap|doldur|gir)|bilgisayarı kullan|oyun|kurulum yap",
    "clipboard": r"pano|kopyala|yapıştır|clipboard",
    "app": r"whatsapp|instagram|insta\b|tiktok|telegram|dm at|mesaj at|discord'?a yaz",
    "youtube": r"youtube|video|klip|izle|şarkı(yı|sını)? (aç|çal)|\bçal\b",
    "media": r"müzik|şarkı|ses seviyesi|sesi (aç|kıs|kapat)|duraklat|devam ettir|sonraki|önceki|\bsesi?\b",
    "notes": r"\bnot\b|notlar|not al|not et",
    "report": r"rapor|özet|sabah",
    "weather": r"hava|yağmur|kar yağ|derece|sıcak|soğuk|şemsiye|rüzgar",
    "feedback": r"beğenmedim|yanlış (yaptın|cevap)|kötü cevap|saçmaladın|öyle deme",
}
_HINT_RX = {name: re.compile(rx, re.I) for name, rx in TOOL_HINTS.items()}
_HINTED_GROUPS = {TOOL_TO_GROUP[n] for n in TOOL_HINTS}

# Bağlantı araçları (alice/connect) çalışma anında tanımlardan geliyor; adları burada tutulur ve
# "connect" grubuna sayılır. Böylece her isteğe 4-5 servisin şeması eklenmez, gerekince açılır.
CONNECT: set[str] = set()


def register_connect(names) -> None:
    CONNECT.clear()
    CONNECT.update(names)
_RX = {g: re.compile(rx, re.I) for g, (_, _, rx) in GROUPS.items()}


# MCP sunucularının araçları (mcp__<sunucu>__<araç>) "mcp:<sunucu>" grubunda; sunucu adı geçince ya da
# aşağıdaki ek kelimelerle açılır. Bilinen sunucular için ek anahtar kelimeler:
MCP_KEYWORDS = {"github": r"git ?hub|\brepo|issue|pull request|\bpr\b|commit|release|branch"}


def all_groups() -> dict[str, tuple[set[str], str, str]]:
    """Yerleşik gruplar + eklenti grupları."""
    return {**GROUPS, **plugins.groups()}


def group_of(name: str) -> str | None:
    if name in TOOL_TO_GROUP:
        return TOOL_TO_GROUP[name]
    if name in CONNECT:
        return "connect"
    for g, (names, _, _) in plugins.groups().items():
        if name in names:
            return g
    if name.startswith("mcp__"):
        return "mcp:" + name.split("__")[1]
    return None


def mcp_servers(tool_names) -> set[str]:
    return {n.split("__")[1] for n in tool_names if n.startswith("mcp__") and n.count("__") >= 2}


def groups_for_text(text: str, tool_names=()) -> set[str]:
    """Kullanıcının sözüne göre açılacaklar: grup adları ve (ipucu olan büyük gruplarda) tek tek araç adları."""
    text = text or ""
    out = {g for g, rx in _RX.items() if g not in _HINTED_GROUPS and rx.search(text)}
    out |= {name for name, rx in _HINT_RX.items() if rx.search(text)}
    out |= {g for g, (_, _, rx) in plugins.groups().items() if rx and re.search(rx, text, re.I)}
    for srv in mcp_servers(tool_names):
        rx = re.escape(srv) + ("|" + MCP_KEYWORDS[srv] if srv in MCP_KEYWORDS else "")
        if re.search(rx, text, re.I):
            out.add("mcp:" + srv)
    return out


def visible_tools(tools: dict[str, Tool], enabled: set[str]) -> list[Tool]:
    """Temel araçlar + açık gruplar + gruba ait olmayanlar (MCP vb.)."""
    out = []
    always = CORE | plugins.always_tools()
    for name, t in tools.items():
        g = group_of(name)
        if name in always or g is None or g in enabled or name in enabled:
            out.append(t)
    return out


def catalog(tools: dict[str, Tool]) -> str:
    """Sistem talimatına eklenen kısa liste: tanımı her istekte gitmeyen araçların adları, gruplarıyla."""
    always = CORE | plugins.always_tools()
    by_group: dict[str, list[str]] = {}
    for name in tools:
        g = group_of(name)
        if name not in always and g is not None:
            by_group.setdefault(g, []).append(name)
    groups = all_groups()
    lines = []
    for g, names in by_group.items():
        desc = groups[g][1] if g in groups else ""
        shown = sorted(names)[:8] + (["…"] if len(names) > 8 else [])
        lines.append(f"- {g} ({', '.join(shown)}): {_clip(desc, 90)}" if desc else f"- {g} ({', '.join(shown)})")
    if not lines:
        return ""
    return ("# Yüklenebilir araçlar\nBunların tanımı her istekte gönderilmiyor. Kullanıcının sözleri çoğu zaman "
            "gerekeni kendiliğinden açar; gereken araç elinde yoksa önce load_tools ile aç (grup ya da araç adı):\n"
            + "\n".join(lines))


def _clip(desc: str, n: int = 62) -> str:
    """Grup açıklamasını kısalt: ilk virgüle kadar olan kısım çoğu zaman yeterli."""
    desc = " ".join(desc.split())
    if len(desc) <= n:
        return desc
    head = desc[:n]
    cut = max(head.rfind(", "), head.rfind(" ve "))
    return (head[:cut] if cut > n // 2 else head.rstrip()) + "…"


class LoadTools(Tool):
    name = "load_tools"
    parallel_safe = True

    description = ("Şu an tanımı yüklü olmayan araçları açar (sistem talimatındaki 'Yüklenebilir araçlar' listesi); "
                   "bir sonraki adımdan itibaren kullanılabilirler. Gereken araç yüklü değilse 'yapamam' demeden önce "
                   "bunu çağır. Grup adı ya da araç adı verilebilir; MCP için 'mcp:<sunucu>', hepsi için 'all'.")

    parameters = obj({"groups": {"type": "array", "items": {"type": "string"},
                                 "description": "Grup ya da araç adları, ör. [\"desktop\", \"youtube\"]."}}, ["groups"])
    risk = READ

    def run(self, ctx: ToolContext, groups: list[str] | str) -> ToolResult:
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.split(",")]
        servers = mcp_servers(ctx.state.get("tool_names", ()))
        groups_ = all_groups()
        known = set(groups_) | {"mcp:" + s for s in servers}
        # Araç adı verildiyse grubunu aç (model listedeki araç adını yazabilir).
        groups = [group_of(g) or g if g not in known else g for g in groups]
        groups = ["mcp:" + g if g in servers else g for g in groups]
        wanted = known if "all" in groups else set(groups)
        unknown = wanted - known
        if unknown:
            raise ToolError(f"bilinmeyen grup: {', '.join(sorted(unknown))}. Gruplar: {', '.join(groups_)}")
        enabled: set[str] = ctx.state.setdefault("tool_groups", set())
        enabled |= wanted
        names = sorted(n for g in wanted if g in groups_ for n in groups_[g][0])
        names += sorted(g for g in wanted if g.startswith("mcp:"))
        return ToolResult(f"açıldı: {', '.join(names)}")


TOOLS: list[Any] = [LoadTools()]
