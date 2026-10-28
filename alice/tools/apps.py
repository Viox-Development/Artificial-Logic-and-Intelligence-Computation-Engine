"""`app` aracı: uygulamayı/siteyi doğru yoldan aç ve tarifteki akışı çalıştır.

Yol seçimi tarifte yazılı (bkz. alice/apps.py): resmî API varsa model oraya yönlendirilir, uygulamanın
derin bağlantısı varsa o kullanılır, kalanı tarayıcı + reflex ile yapılır.
"""

from __future__ import annotations

import re
from typing import Any

from alice.apps import App, find, registry
from alice.tools.base import COMPUTER, READ, SEND, Tool, ToolContext, ToolError, ToolResult, obj


def _open_web(ctx: ToolContext, url: str) -> str:
    """Kullanıcının kendi tarayıcısında aç (oturumları orada açık). Bağlanamazsa varsayılan tarayıcı."""
    from alice.tools.browser import Browser

    try:
        # browser aracının kendi mantığı: site zaten bir sekmedeyse ona geç, yoksa aç.
        return Browser().run(ctx, action="goto", url=url).text
    except Exception as e:  # noqa: BLE001 - tarayıcıya bağlanamamak işin sonu değil
        from alice.tools.desktop import open_app

        ctx.emit(f"tarayıcıya bağlanılamadı ({e}), varsayılan tarayıcıyla açılıyor")
        return open_app(url)


WINDOW_WAIT = 20.0  # saniye: uygulama açılınca penceresinin gelmesi için en fazla
# Tarayıcı pencereleri: "(7) WhatsApp - Opera" sekmesi masaüstü WhatsApp'ın penceresi sanılmasın.
BROWSER_TITLE_RX = re.compile(r" - (Opera|Google Chrome|Microsoft.?.?.? ?Edge|Mozilla Firefox|Brave|Vivaldi|Yandex)( |$)")
_installed: dict[str, bool] = {}


def _app_windows(app: App) -> list[str]:
    from alice.tools.desktop import list_windows

    want = app.window_title.lower()
    return [w for w in list_windows() if want in w.lower() and not BROWSER_TITLE_RX.search(w)]


def desktop_available(app: App) -> bool:
    """Uygulama açık ya da kurulu mu? Kurulu olma bilgisi süreç boyunca hatırlanır (ilk arama ~2 sn)."""
    try:
        if _app_windows(app):
            return True
    except Exception:  # noqa: BLE001 - pencere listesi alınamazsa kurulu olup olmadığına bak
        pass
    key = (app.exe or app.label).lower()
    if key not in _installed:
        from alice.tools.desktop import find_installed_apps

        try:
            _installed[key] = bool(find_installed_apps(app.exe or app.label, limit=1))
        except Exception:  # noqa: BLE001
            _installed[key] = False
    return _installed[key]


def preferred(app: App, apps: dict[str, App]) -> App:
    """Web kaydının masaüstü karşılığı kuruluysa onu kullan: WhatsApp Web'in oturumu kapalıydı (QR istiyordu) ama
    kullanıcının masaüstü uygulaması açıktı ve oturumu vardı."""
    alt = apps.get(app.desktop) if app.desktop else None
    return alt if alt is not None and desktop_available(alt) else app


def _ensure_window(app: App, wait: float = WINDOW_WAIT, sleep=None) -> str:
    """Uygulamanın penceresi yoksa aç, gelmesini bekle, öne al. Bulunan pencere başlığını döndürür."""
    import time

    from alice.tools.desktop import focus_window, open_app

    sleep = sleep or time.sleep

    def find_title() -> str | None:
        return next(iter(_app_windows(app)), None)

    title = find_title()
    if title is None:
        open_app(app.exe or app.label)
        end = time.monotonic() + wait
        while title is None and time.monotonic() < end:
            sleep(0.5)
            title = find_title()
        if title is None:
            raise ToolError(f"{app.label} açıldı ama '{app.window_title}' başlıklı pencere {int(wait)} sn içinde "
                            "gelmedi; tarifte window alanını pencerenin gerçek başlığına göre ayarla")
    focus_window(title)
    return title


def _flow(ctx: ToolContext, app: App, flow: str, args: dict[str, Any], max_steps: int) -> str:
    from alice.reflex.loop import ReflexLoop, make_text_writer
    from alice.reflex.observers import BrowserObserver, DesktopObserver

    if ctx.decider is None or ctx.router is None:
        raise ToolError("karar motoru yapılandırılmamış (alice doctor)")
    try:
        goal = app.goal(flow, args)
    except ValueError as e:
        raise ToolError(str(e)) from e
    if app.flow_target == "desktop":
        _ensure_window(app)
        observer = DesktopObserver(ctx, app.window_title)
    else:
        observer = BrowserObserver(ctx, app.url or None)
    loop = ReflexLoop(observer, ctx.decider, make_text_writer(ctx.router),
                      min_confidence=float(ctx.config.decider.get("min_confidence", 0.55)), emit=ctx.emit)
    return loop.run(goal, max_steps=max_steps).report()


def _describe(apps: dict[str, App]) -> str:
    rows = []
    for a in sorted(apps.values(), key=lambda x: x.name):
        bits = [a.name, f"({a.kind}" + (", masaüstü" if a.flows and a.flow_target == "desktop" else "") + ")"]
        if a.desktop:
            bits.append(f"masaüstü kuruluysa: {a.desktop}")
        if a.api:
            bits.append(f"API: {a.api}")
        if a.flows:
            bits.append("akışlar: " + ", ".join(f"{f}({', '.join(a.fields(f))})" for f in sorted(a.flows)))
        rows.append(" · ".join(bits))
    return "\n".join(rows) or "kayıt yok"


class AppTool(Tool):
    name = "app"
    description = (
        "Bir uygulamayı ya da siteyi doğru yoldan açar ve tanımlı akışları çalıştırır (WhatsApp'tan mesaj atma, "
        "Instagram DM, YouTube'da video açma, Steam'de oyun başlatma gibi).\n"
        "- list: hangi uygulamalar tanımlı, hangi akışları ve hangi alanları var.\n"
        "- open: uygulamayı/siteyi aç (site ise kullanıcının kendi tarayıcısında, oturumu açık olan yerde).\n"
        "- flow: tanımlı akışı çalıştır, ör. app(action='flow', name='whatsapp', flow='send', "
        "args={'kime': 'Ahmet', 'mesaj': 'geliyorum'}).\n"
        "- Bir uygulamanın resmî bağlantısı (API) varsa list çıktısında yazar; o varsa onu tercih et, daha hızlı.\n"
        "- Burada olmayan bir sayfada iş yapacaksan browser ya da reflex araçlarını kullan."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "open", "flow"],
                   "description": "list: tanımlı uygulamalar | open: aç | flow: tanımlı akışı çalıştır."},
        "name": {"type": "string", "description": "Uygulama adı, ör. 'whatsapp', 'tiktok', 'steam_game'. "
                 "Takma adlar da çalışır ('wp', 'insta')."},
        "flow": {"type": "string", "description": "flow için akış adı, ör. 'send'. list çıktısında görünür."},
        "args": {"type": "object", "additionalProperties": {"type": "string"},
                 "description": "Akışın alanları, ör. {\"kime\": \"Ahmet\", \"mesaj\": \"selam\"}."},
        "target": {"type": "string", "description": "protocol türü için yer tutucu değeri, ör. steam_game'de appid."},
        "max_steps": {"type": "integer", "description": "flow için en fazla reflex adımı. Varsayılan 20."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        if args.get("action") == "list":
            return READ
        if args.get("action") == "flow":
            app = find(str(args.get("name", "")))
            if app is not None and str(args.get("flow", "")) in app.sends:
                return SEND  # mesaj/DM/e-posta: başkasına gidiyor, geri alınamaz
        return COMPUTER

    def summary(self, args: dict[str, Any]) -> str:
        a = args.get("action", "")
        if a == "list":
            return "tanımlı uygulamalar"
        name = args.get("name", "")
        return f"{name} · {args.get('flow')}" if a == "flow" else f"{name} aç"

    def run(self, ctx: ToolContext, action: str, name: str = "", flow: str = "", args: dict[str, Any] | None = None,
            target: str = "", max_steps: int = 20) -> ToolResult:
        apps = registry(ctx.config)
        if action == "list":
            return ToolResult(_describe(apps))
        app = find(name, apps=apps)
        if app is None:
            raise ToolError(f"'{name}' diye bir uygulama tanımı yok. Tanımlılar: {', '.join(sorted(apps))}. "
                            "Yenisini ~/.alice/apps.toml içine ekleyebilirsin.")
        chosen = preferred(app, apps)
        if chosen is not app and (action != "flow" or flow in chosen.flows):
            ctx.emit(f"{app.label}: masaüstü uygulaması kurulu, onunla ({chosen.label})")
            app = chosen
        if action == "flow":
            if not app.flows:
                raise ToolError(f"{app.label} için tanımlı akış yok; open ile açıp browser/reflex ile devam et.")
            note = f"\n({app.note})" if app.note else ""
            return ToolResult(_flow(ctx, app, flow, args or {}, max_steps) + note)
        # open
        if app.kind == "web":
            out = _open_web(ctx, app.url)
        elif app.kind == "protocol":
            from alice.apps import FIELD_RX

            if FIELD_RX.search(app.url) and not target.strip():
                raise ToolError(f"{app.label}: target gerekli, şablon: {app.url}")
            url = FIELD_RX.sub(lambda _m: target.strip(), app.url)
            from alice.tools.desktop import open_app

            out = open_app(url)
        else:
            from alice.tools.desktop import open_app

            out = open_app(app.exe or app.label)
        extra = []
        if app.api:
            extra.append(f"not: bu servisin resmî bağlantısı var ({app.api}), veri işleri için onu kullan")
        if app.note:
            extra.append(app.note)
        return ToolResult(out + ("\n" + "\n".join(extra) if extra else ""))


TOOLS = [AppTool()]
