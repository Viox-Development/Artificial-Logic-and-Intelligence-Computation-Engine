"""Tool groups: instead of sending definitions for all ~90 tools (~42K tokens) on every request, send only what is needed.

Core tools always go. Other groups (or, for large groups, single tools) are added via keywords in the user's message
filtered by the context (code project or not, voice), tools used recently in the conversation, or when the model
requests via `load_tools`, and stay for the rest of the session.
Smaller request = fits more free models, faster, and less confusing. `python -m alice.tools.measure` prints the sizes.
"""

from __future__ import annotations

import json
import re
from typing import Any

from alice import plugins
from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult, obj

# Tools sent with full definition on every request: the most frequently needed ones. Others (like in Claude Code)
# are only listed by name in the system prompt; they are loaded automatically based on the user's words or when
# the model calls load_tools. Previously, 27 tools took ~9K tokens on every request: narrow-context/fast free models
# (Groq, Cerebras) could not be the main model.
CORE = {"read_file", "write_file", "edit_file", "list_dir", "glob", "grep", "run_command", "web_fetch", "web_search",
        "memory", "load_tools", "ask_user"}

# group -> (tools, short description, keyword regex)
GROUPS: dict[str, tuple[set[str], str, str]] = {
    "files": ({"file_ops", "multi_edit", "apply_patch", "notebook_edit", "find_files", "send_file", "next_step",
               "share", "permissions", "hooks", "usage", "code_intel", "mcp_resources"},
              "moving/copying/deleting files, finding personal files (downloads, recently opened), many changes in "
              "one file, patching many files, Jupyter notebooks, showing a file to the user",
              r"taşı|kopyala|\bsil(in|e|er misin)?\b|yeniden adlandır|adını değiştir|çöp|düzenle|değiştir|düzelt|refactor|"
              r"yama|patch|notebook|ipynb|jupyter|\bkod|code|script|fonksiyon|\.py\b|\.js\b|\.ts\b|bug|klasör|"
              r"indirdiğim|indirilen|açtığım|nerede(ydi)?\b|\bpdf|excel|word dosya|belge(yi|si|m)|toparla|"
              r"paylaş|bağlantı(sını)? ver|link(ini)? ver|izin(ler)?|artık sorma|sormadan|\bhook|her (dosya )?düzenleme|"
              r"token|kullanım|nereye gitti|harcadı|\bmcp\b|lint|tip hata|derleme hata|tanım(ı|landığı)|nerede kullanıl|"
              r"sembol|referans"),
    "plan": ({"todo_write", "next_step", "monitor", "session", "run_plan", "task_control", "task", "subagents",
              "agent_defs", "process_output", "process_stop", "process_wait", "skill", "exit_plan_mode",
              "enter_plan_mode", "notify", "advisor"},
             "multi-step work plan and to-do list, end-of-turn summary box, subagents, starting/handing off a "
             "separate chat, watching in the background and coming back on an event, output/waiting/stopping of a "
             "background command, skills, plan approval",
             r"önce .* sonra|ardından|sonra da|adım adım|planla|alt ajan|ajan tanımla|ajanlar(ım)?\b|arka planda|"
             r"background|sunucu(yu)? başlat|"
             r"dev server|npm run|\bwatch\b|beceri|skill|bitince|bitti(ğ|g)inde|haber ver|izle|ayrı (sohbet|oturum)|"
             r"paralel|devret|danış|advisor|ikinci görüş"),
    "companions": ({"buddy", "kairos"},
                   "BUDDY (little companion: status, pet, feed, name it) and KAIROS (background watch: status, on/off, "
                   "wake now)",
                   r"\bbuddy|bad[iı]\b|tamagotchi|evcil|minik arkadaş|kairos|nöbet|arka plan(da)? (bak|kolla)|"
                   r"kendi kendine uyan"),
    "desktop": ({"computer", "computer_task", "ui_elements", "ui_act", "clipboard", "reflex", "app", "windows", "game"},
                "opening/closing apps and windows, screenshots, mouse/keyboard, clicking in desktop apps, clipboard, "
                "game session (rounds, notes)",
                r"tıkla|ekran|pencere|fare|mouse|klavye|screenshot|buton|düğme|menü|click|screen|pano|kopyala|"
                r"yapıştır|clipboard|oyun|ayarlar|uygulamada|arayüz|simge|ikon|whatsapp|instagram|insta\b|"
                r"tiktok|dm at|mesaj at|uygulama|\baç|kapat|başlat|küçült|sekme|sıra tabanlı|strateji oyunu|"
                r"tur(u|unu)? (oyna|bitir|geç)"),
    "browser": ({"browser", "preview"}, "reading web pages, clicking, filling forms (in ALICE's built-in browser, the user watches live in "
                                        "the panel); opening a web interface you are building and checking its errors",
                r"site|tarayıcı|browser|chrome|edge|firefox|opera|google|sayfa|link|https?://|www\.|\.com|"
                r"giriş yap|login|online|internetten|instagram|twitter|github|e-?posta|mail|localhost|dev server|"
                r"önizle|preview|arayüz(ü|de)? (dene|kontrol|doğrula)|sayfayı aç"),
    "github": ({"git", "github", "code_review"},
               "git (commit, branch, push), GitHub (opening/merging PRs, CI status/logs/re-runs, answering review "
               "comments, watching a PR in the background and fixing it) and independent code review",
               r"git\b|git ?hub|\brepo|depo|commit|push|pull|pull request|\bpr\b|merge|birleştir|branch|\bdal\b|"
               r"issue|klonla|clone|\bci\b|review|incele|gözden geçir|kod kontrol|yorum(lar)?a? (bak|cevap)"),
    "system": ({"system_info", "process_list", "process_kill", "metrics"},
               "CPU/RAM/GPU/disk status, running processes, past measurements",
               r"cpu|ram|gpu|işlemci|ekran kartı|sıcaklık|derece|bellek|disk|pil|batarya|performans|işlem|süreç|"
               r"process|görev yöneticisi|kasıyor|yavaş|fps|metrik"),
    "diagnostics": ({"app_state", "service_diagnose", "test_run"},
                    "the app/agent's current state (mode, loaded tool groups, background processes), health/logs of the "
                    "ALICE core service and background processes, running tests and showing only the summary and errors",
                    r"app_state|uygulama durumu|ajan durum|çalışıyor mu|ne yapıyor|durum raporu|servis|log(lar)?ı?|"
                    r"durduruldu|çalışmıyor|crash|çöktü|patladı|test(leri|lerimi)? (çalıştır|yap|koş)|pytest|unittest|"
                    r"birim test|entegrasyon test|teşhis|diagnos|hata ayıkla|teşhis et"),
    "automation": ({"rules", "schedule", "feeds", "action_log"},
                   "alert rules, scheduled tasks, service feeds and the log of what was sent",
                   r"kural|uyar|hatırlat|zamanla|her gün|her sabah|her akşam|haftada|alarm|schedule|rule|cron|"
                   r"saat \d|otomatik|haber ver|söyle bana|gelirse|girince|olunca|bildir|takip et|izle|"
                   r"ne gönderdin|kime yazdın|ne yazdın|benim adıma|gönderdiklerin"),
    "blender": ({"blender"}, "3D scenes in Blender (bpy script)",
                r"blender|3d|3b|üç boyut|modelle|sahne|render|mesh"),
    "selftools": ({"make_tool"}, "write a new tool for a job you can't do (tested in a sandbox, added on approval), list the tools you "
                                 "wrote",
                  r"araç(ın)? yaz|aracın yok|yapamıyor|eklenti yaz|kendin yaz|hangi araçları"),
    "now": ({"now"}, "the user's current state: mode, game, today's flights, heart rate, advice",
            r"nasılım|ne durumdayım|durumum|mola|yorgun muyum|bugün nasıl gidiyor"),
    "timeline": ({"timeline"}, "time machine memory: search text seen on the screen before",
                 r"ekranda gör|geçen (hafta|gün).*(link|bağlantı|yazı|mesaj)|o linki bul|zaman çizgisi|"
                 r"daha önce gördü|okuduğum o"),
    "flight": ({"flight", "dcs_wingman"}, "DCS / War Thunder flight data, kneeboard, orders to DCS wingmen",
               r"dcs|war ?thunder|uçuş|uçak|kneeboard|pist|iniş|kalkış|simülasyon|sim\b|kanat adam|wingman|"
               r"\bsam\b|bastır|üsse dön"),
    "connect": (set(),  # tools come from definitions at runtime (alice/connect)
                "connected services: Spotify, Steam, Discord, YouTube, APIs you added",
                r"spotify|müzik|şark[ıi]|parça|çal\b|duraklat|steam|oyun kütüphanesi|discord|sunucu|kanal|"
                r"abone|izlenme|playlist|çalma listesi"),
    "net": ({"http_request", "web_crawl"}, "calling REST APIs, webhooks, sending JSON, downloading a file from a URL; researching a site by "
                                           "going through several pages (web_crawl)",
            r"\bapi\b|endpoint|webhook|\bjson\b|\brest\b|http isteği|post isteği|istek at|indir|download|curl|"
            r"crawl|tara\b|siteyi (gez|tara|araştır)|dokümantasyon|docs?\b|belgelerinde|sitede (ara|bul)|"
            r"birkaç sayfa|tüm sayfa"),
    "team": ({"team", "council"}, "splitting big jobs into an autonomous agent project, and the review council (experts discuss and "
                                  "vote)",
             r"takım|ekip|paralel|aynı anda|ajanlar|ajan çağır|orkestra|alt ajan|subagent|multi.?agent|büyük (iş|proje|"
             r"kod)|codebase|kod tabanı|refactor|yeniden düzenle|kurul|eleştir|tartış|savun|artı.*eksi|incele"),
    "personal": ({"notes", "report", "media", "youtube", "weather", "feedback", "phone_call"},
                 "notes, morning/PC report, music, playing videos on YouTube, volume, weather, disliked answers, "
                 "calling the user's phone",
                 r"\bnot\b|notlar|not al|rapor|özet|sabah|müzik|şarkı|ses seviyesi|sesi aç|sesi kıs|spotify|"
                 r"kapat .*dakika|dakika sonra|youtube|video|klip|izle|\bçal\b|hava|yağmur|kar yağ|derece|sıcak|soğuk|"
                 r"şemsiye|beğenmedim|yanlış (yaptın|cevap)|kötü cevap|beni ara|ara beni|telefon(la|dan)? ara"),
    "projects": ({"projects"}, "the user's projects (repo, Unity, Python...) and their details",
                 r"proje|repo\b|depo(su|m|yu)?\b|unity|godot|kodum|oyunum|oyunumu|uygulamam|scriptim|programım|"
                 r"son commit|nerede duruyor|hangi klasör"),
    "mail": ({"gmail"}, "Gmail (several accounts): search, read, draft, send email",
             r"e-?posta|\bmail|gmail|gelen kutu|inbox|okunmamış|taslak|cevap yaz|yanıtla"),
    "home": ({"pc", "wifi", "android", "tv"}, "PC power/brightness, Wi-Fi and the home network, TV, Android phone",
             r"kilitle|uyut|uyku|hazırda|yeniden başlat|restart|parlaklı|monitör|ekranı kapat|güç plan|wi-?fi|"
             r"kablosuz|internet|modem|\bağ(a|da|ı|daki)?\b|ssid|\btv\b|televizyon|android|telefon|bildirim|"
             r"\bpil\b|şarj|wake.?on|uzaktan aç|\bmac\b|netflix|samsung|kanal"),
}

TOOL_TO_GROUP = {name: g for g, (names, _, _) in GROUPS.items() for name in names}

# In large groups, open only the tool that is needed, not the whole group: for "YouTube'da lofi aç" (play lofi on
# YouTube) the youtube tool is enough, not the 8 desktop tools or the notes/report tools. Here the tools of named
# groups open only by their own patterns; the whole group opens on load_tools or when one of its tools is used.
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
    "media": r"müzik|şarkı|ne çal|çalıyor|çalan|dinliyorum|ses seviyesi|sesi (aç|kıs|kapat)|duraklat|devam ettir|sonraki|önceki|\bsesi?\b",
    "notes": r"\bnot\b|notlar|not al|not et",
    "report": r"rapor|özet|sabah",
    "weather": r"hava|yağmur|kar yağ|derece|sıcak|soğuk|şemsiye|rüzgar",
    "feedback": r"beğenmedim|yanlış (yaptın|cevap)|kötü cevap|saçmaladın|öyle deme",
    "phone_call": r"beni ara|ara beni|telefon(la|dan|uma)? ara|arar mısın|arayabilir misin|çaldır",
    "game": r"sıra tabanlı|strateji oyunu|tur(u|unu)? (oyna|bitir|geç)|oyun oturumu",
    # files: a coding request used to open all 13 tools (~5K tokens) on words like "kod", "bug", ".py"; edit_file
    # in the core covers ordinary edits, so only the tool a word really points at opens.
    "file_ops": r"taşı|kopyala|\bsil(in|e|er misin)?\b|yeniden adlandır|adını değiştir|çöp|toparla|"
                r"\b(move|rename|delete|copy) (the |this |these )?(file|folder)",
    "multi_edit": r"refactor|yeniden düzenle|birkaç (yer|değişiklik)|çok (yerde|değişiklik)|several (changes|places)",
    "apply_patch": r"refactor|yeniden düzenle|\byama|\bpatch|(birden )?(çok|fazla|birkaç) dosya|several files|"
                   r"across (the )?(files|codebase)|everywhere",
    "notebook_edit": r"notebook|ipynb|jupyter",
    "find_files": r"indirdiğim|indirilen|açtığım|nerede(ydi)?\b|\bpdf|excel|word dosya|belge(yi|si|m)\b|"
                  r"downloads?\b|recent files|where is (my|the) file",
    "send_file": r"dosya(yı|sını)? (gönder|göster|ver)|bana (gönder|göster)|send (me )?the file|show me the file",
    "share": r"paylaş|bağlantı(sını)? ver|link(ini)? ver|\bshare\b",
    "permissions": r"izin(ler)?\b|artık sorma|sormadan|permission",
    "hooks": r"\bhook|her (dosya )?düzenleme(de|den)",
    "usage": r"token|kullanım|nereye gitti|harcadı|\busage\b|\bcost\b",
    "code_intel": r"lint|tip hata|derleme hata|tanım(ı|landığı)|nerede kullanıl|sembol|referans|type error|"
                  r"compile error|definition|references|where is .* used",
    "mcp_resources": r"\bmcp\b",
    # plan: "önce X sonra Y" used to open 17 tools (~6K tokens).
    "todo_write": r"önce .* sonra|ardından|sonra da|adım adım|planla|step by step|\bthen\b",
    "run_plan": r"önce .* sonra|ardından|sonra da|adım adım|step by step",
    "task_control": r"görev(i)? (onayla|durdur|iptal)|planı (onayla|durdur)|ne durumda",
    "task": r"alt ajan|subagent|paralel|parallel|aynı anda",
    "subagents": r"alt ajan|subagent|ajanlar(ım)?\b",
    "agent_defs": r"ajan tanımla|ajan(ı|ları)? (oluştur|düzenle)|agent definition",
    "process_output": r"arka planda|background|sunucu(yu)? başlat|dev server|npm run|\bwatch\b|start the server",
    "process_stop": r"arka planda|background|sunucu(yu)? (başlat|durdur)|dev server|npm run|\bwatch\b|"
                    r"stop the server",
    "process_wait": r"arka planda|background|bitince|bitti(ğ|g)inde|wait for (the )?(process|command|build|server)",
    "monitor": r"\bizle\b|\bwatch\b|bitince|bitti(ğ|g)inde|haber ver|monitor",
    "notify": r"haber ver|bildir|notify",
    "session": r"ayrı (sohbet|oturum)|devret|new session|hand ?off",
    "skill": r"beceri|\bskill",
    "enter_plan_mode": r"plan modu|plan mode|önce plan",
    "exit_plan_mode": r"plan modu|plan mode",
    "advisor": r"danış|advisor|ikinci görüş|second opinion",
    "next_step": r"sıradaki adım|next step",
    # github: commit words don't need the PR/CI tool or the reviewer, and vice versa.
    "git": r"git\b|commit|push|pull\b|merge|birleştir|branch|\bdal\b|\bdiff\b|stash|rebase|worktree|klonla|clone",
    "github": r"git ?hub|\brepo\b|pull request|\bpr\b|issue|\bci\b|klonla|clone|release|actions",
    "code_review": r"review|\bincele|gözden geçir|kod kontrol|yorum(lar)?a? (bak|cevap)",
    # team: "refactor" or "codebase" alone is not a reason to start an agent project.
    "team": r"takım|ekip|ajanlar(ı)? (çağır|başlat)|orkestra|multi.?agent|büyük (iş|proje)|\bteam\b",
    "council": r"kurul|eleştir|tartış|savun|artı.*eksi|council|pros and cons",
    # diagnostics
    "test_run": r"test(leri|lerimi)? (çalıştır|yap|koş)|pytest|unittest|birim test|entegrasyon test|"
                r"run (the )?tests?|failing test|tests? (fail|pass)|testler(i)?\b",
    "app_state": r"app_state|uygulama durumu|ajan durum|çalışıyor mu|ne yapıyor|durum raporu",
    "service_diagnose": r"servis|\blog(lar)?ı?\b|durduruldu|çalışmıyor|crash|çöktü|patladı|teşhis|diagnos|"
                        r"hata ayıkla",
}
_HINT_RX = {name: re.compile(rx, re.I) for name, rx in TOOL_HINTS.items()}
_HINTED_GROUPS = {TOOL_TO_GROUP[n] for n in TOOL_HINTS}

# Connection tools (alice/connect) come from definitions at runtime; their names are kept here and counted in the
# "connect" group, so the schemas of 4-5 services are not added to every request; they open when needed.
CONNECT: set[str] = set()


def register_connect(names) -> None:
    CONNECT.clear()
    CONNECT.update(names)
_RX = {g: re.compile(rx, re.I) for g, (_, _, rx) in GROUPS.items()}


# MCP server tools (mcp__<server>__<tool>) live in the "mcp:<server>" group; they open when the server name
# is mentioned or with the extra words below. Extra keywords for known servers:
MCP_KEYWORDS = {"github": r"git ?hub|\brepo|issue|pull request|\bpr\b|commit|release|branch"}


def all_groups() -> dict[str, tuple[set[str], str, str]]:
    """Built-in groups + plugin groups."""
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
    """What to open based on the user's words: group names and, for large groups with hints, individual tool names."""
    text = text or ""
    out = {g for g, rx in _RX.items() if g not in _HINTED_GROUPS and rx.search(text)}
    out |= {name for name, rx in _HINT_RX.items() if rx.search(text)}
    out |= {g for g, (_, _, rx) in plugins.groups().items() if rx and re.search(rx, text, re.I)}
    for srv in mcp_servers(tool_names):
        rx = re.escape(srv) + ("|" + MCP_KEYWORDS[srv] if srv in MCP_KEYWORDS else "")
        if re.search(rx, text, re.I):
            out.add("mcp:" + srv)
    return out


# Tools that only make sense for code. Outside a code project (home folder, voice) words like "düzelt" (fix),
# "değiştir" (change) or "incele" (look at) are about documents, settings or videos far more often than code, so
# keyword guesses don't open these there; load_tools still does.
CODE_TOOLS = {"multi_edit", "apply_patch", "code_intel", "code_review", "test_run", "notebook_edit"}

# Files that mark a directory as a code project.
PROJECT_MARKERS = (".git", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "package.json", "Cargo.toml",
                   "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "CMakeLists.txt", "Makefile",
                   "composer.json", "Gemfile", "deno.json", "pubspec.yaml", "mix.exs", "project.godot")
PROJECT_GLOBS = ("*.sln", "*.csproj", "*.uproject")
RECENT_MESSAGES = 60  # tools called in this many latest messages stay loaded (e.g. after reopening a saved chat)


def is_project(cwd) -> bool:
    """Is cwd a code project (markers in it, or inside a git repo), rather than the home folder or a plain folder?"""
    from pathlib import Path

    try:
        cwd = Path(cwd).resolve()
        home = Path.home().resolve()
        if cwd == home:
            return False
        if any((cwd / m).exists() for m in PROJECT_MARKERS) or any(next(cwd.glob(g), None) for g in PROJECT_GLOBS):
            return True
        for d in cwd.parents:
            if d == home:
                return False  # a dotfiles repo in the home folder doesn't make every folder a project
            if (d / ".git").exists():
                return True
    except OSError:
        pass  # unreadable folder (permissions, network drive gone): treat it as not a project
    return False


def recent_tools(messages, limit: int = RECENT_MESSAGES) -> set[str]:
    """Names of the tools called in the last `limit` messages."""
    out: set[str] = set()
    for m in list(messages)[-limit:]:
        for c in m.get("tool_calls") or []:
            name = (c.get("function") or {}).get("name") if isinstance(c, dict) else None
            if isinstance(name, str) and name:
                out.add(name)
    return out


def select_groups(text: str, *, cwd=None, mode: str = "", tool_names=()) -> set[str]:
    """Groups and single tools to open for a message, from simple rules (no model call): keywords in the message,
    filtered by the context (code project or not, voice)."""
    out = groups_for_text(text, tool_names)
    if out & CODE_TOOLS and (mode == "voice" or cwd is None or not is_project(cwd)):
        out -= CODE_TOOLS
    return out


def visible_tools(tools: dict[str, Tool], enabled: set[str]) -> list[Tool]:
    """Core tools + open groups + tools that belong to no group (MCP etc.)."""
    out = []
    always = CORE | plugins.always_tools()
    for name, t in tools.items():
        g = group_of(name)
        if name in always or g is None or g in enabled or name in enabled:
            out.append(t)
    return out


def catalog(tools: dict[str, Tool]) -> str:
    """Short system-prompt list: names (with groups) of tools whose definitions are not sent on each request."""
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
    return ("# Loadable tools\nTheir definitions aren't sent with every request, but you can call them directly by "
            "name: it runs and the definition is added from the next requests on. If you want to see their parameters "
            "first, use load_tools (a group or tool name):\n"
            + "\n".join(lines))


def _clip(desc: str, n: int = 62) -> str:
    """Shorten a group description: the part up to the first comma is usually enough."""
    desc = " ".join(desc.split())
    if len(desc) <= n:
        return desc
    head = desc[:n]
    cut = max(head.rfind(", "), head.rfind(" ve "))
    return (head[:cut] if cut > n // 2 else head.rstrip()) + "…"


class LoadTools(Tool):
    name = "load_tools"
    parallel_safe = True

    description = ("Opens tools whose definitions are not loaded yet (the 'Loadable tools' "
                   "list in the system instructions); they can be used from the next step on. Call this before saying "
                   "'I can't' when a needed tool is not loaded. Pass a group name or a tool name; for MCP "
                   "'mcp:<server>'. Ask only for the groups you need ('all' is not accepted).")

    parameters = obj({"groups": {"type": "array", "items": {"type": "string"},
                                 "description": "Group or tool names, e.g. [\"desktop\", \"youtube\"]."}}, ["groups"])
    risk = READ

    def run(self, ctx: ToolContext, groups: list[str] | str) -> ToolResult:
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.split(",")]
        servers = mcp_servers(ctx.state.get("tool_names", ()))
        groups_ = all_groups()
        known = set(groups_) | {"mcp:" + s for s in servers}
        # If a tool name was given, open its group (the model might write the tool name from the list).
        groups = [group_of(g) or g if g not in known else g for g in groups]
        groups = ["mcp:" + g if g in servers else g for g in groups]
        if any(str(g).strip().lower() in ("all", "*", "hepsi") for g in groups):
            # Bütün grupları açmak araç listesini birkaç kat büyütüyor, istemi şişiriyor ve önbelleği baştan
            # yazdırıyordu. Gereken grubu seçsin.
            raise ToolError("'all' kabul edilmiyor: bütün araçları açmak her isteği şişirir. İşin için gereken "
                            f"grupları tek tek iste. Gruplar: {', '.join(groups_)}"
                            + (f"; MCP: {', '.join('mcp:' + s for s in servers)}" if servers else ""))
        wanted = set(groups)
        unknown = wanted - known
        if unknown:
            raise ToolError(f"bilinmeyen grup: {', '.join(sorted(unknown))}. Gruplar: {', '.join(groups_)}")
        enabled: set[str] = ctx.state.setdefault("tool_groups", set())
        enabled |= wanted
        names = sorted(n for g in wanted if g in groups_ for n in groups_[g][0])
        names += sorted(g for g in wanted if g.startswith("mcp:"))
        text = f"açıldı: {', '.join(names)}"
        agent = ctx.state.get("agent")
        deferred = agent.deferred_tools() if agent is not None and hasattr(agent, "deferred_tools") else set()
        if deferred:
            # Önbellek sıcak: tanımlar araç listesine sonra girer (listeyi değiştirmek bütün konuşmayı önbelleğe
            # yeniden yazdırır). Model parametreleri buradan görüp aracı adıyla çağırabilir.
            specs = [agent.tools[n].spec()["function"] for n in sorted(deferred) if n in agent.tools]
            text += ("\nTanımlar (araçlar adıyla çağrılabilir):\n"
                     + "\n".join(json.dumps({"name": s.get("name"), "description": s.get("description"),
                                             "parameters": s.get("parameters")}, ensure_ascii=False) for s in specs))
        return ToolResult(text)


TOOLS: list[Any] = [LoadTools()]
