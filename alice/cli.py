"""ALICE komut satırı.

  alice                      etkileşimli oturum (REPL)
  alice -p "görev"           tek seferlik çalıştır, sonucu yaz, çık
  alice init                 ~/.alice altında config ve .env oluştur
  alice doctor               anahtarları, modelleri ve opsiyonel bağımlılıkları test et
  alice models [--refresh] [--write]  ücretsiz modelleri canlı ölç/sırala; --write rolleri auto yapar
  alice reflex "hedef"       Jev/System One reflex döngüsünü tek başına çalıştır
  alice app                  masaüstü uygulamasını aç
  alice update [--check]     GitHub'daki son sürüme güncelle (main'e merge olanı çeker)
  alice connect [ad]         servis bağlantıları (Spotify, Steam, Discord, YouTube, kendi API'ların)
  alice feeds                bağlı servisleri izleyip yeni bir şey olunca haber veren akışlar
  alice shortcut             masaüstüne ve Başlat menüsüne ALICE kısayolu koy (Windows)
  alice core run|install|status|mode|stats   arka plan çekirdeği (izleme, kurallar, modlar, görevler)
  alice rules [add "cümle"]  uyarı kuralları ("GPU uzun süre ısınırsa söyle")
  alice tasks [add "cümle"]  zamanlanmış görevler ("her pazar gece projelerimi yedekle")
  alice voice [audition|use|library|say|test|status]   sesli asistan ve ses seçimi
  alice quick [--open]       hızlı bar kısayolları (Ctrl+Alt+A / S / C)
  alice dcs install|status|kneeboard   DCS bağlantısı
  alice watch [status|import <db>]     akıllı saat verisi ve ne sıklıkla geldiği
  alice phone                telefon köprüsü (telefondan ALICE, canlı nabız)
  alice notes | brief | report         notlar, sabah özeti, haftalık bilgisayar raporu
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from alice import __version__
from alice.agent import Agent
from alice.config import Config, alice_home, init_home, load_config
from alice.decider import Decider
from alice.memory import attach_memory, shared_memory
from alice.permissions import MODES, Permissions
from alice.providers.router import AllProvidersFailed, Router, parse_ref
from alice.tools import ToolContext, all_tools, attach_mcp, cleanup
from alice.tools.base import ToolResult

console = Console(highlight=False)

HELP = """[bold]Komutlar[/]
  /help                       bu yardım
  /plan                       plan modu: sadece okur/araştırır, değişiklik yapmadan plan yazar
  /mode [plan|ask|auto|yolo]  izin modunu göster/değiştir (Shift+Tab ile de döner)
  /model                      rol zincirlerini ve durumlarını göster
  /model <rol> <ref>          rolü tek modele sabitle, ör. /model brain groq:openai/gpt-oss-120b
  /model <rol> reset          config'teki zincire geri dön
  /rewind                     önceki bir mesaja geri sar (ALICE'in değiştirdiği dosyalar dahil)
  /init                       bu proje için ALICE.md talimat dosyası oluştur
  /usage                      bugünkü istek/token kullanımı
  /tools, /mcp                araçlar / MCP sunucularından gelen araçlar
  /memory                     kalıcı hafızayı göster
  /compact                    context'i şimdi özetle
  /clear                      konuşmayı sıfırla
  /sessions, /resume [id]     kayıtlı oturumlar / oturumu geri yükle
  /cwd [yol]                  çalışma klasörünü göster/değiştir
  /update [apply]             GitHub'daki son sürümü kontrol et / çek
  /exit                       çık
Özel komutlar: ~/.alice/commands/*.md ya da projede .alice/commands/*.md ($ARGUMENTS yer tutucusu).
Mesajda [bold]@dosya/yolu[/] yazarsan dosyanın içeriği mesaja eklenir.
Çalışırken [bold]Ctrl+C[/] mevcut işi durdurur. Fareyi ekranın sol üst köşesine götürmek fare/klavye kontrolünü keser."""


COMPACT_TOOLS = {"read_file", "list_dir", "glob", "grep", "web_search", "web_fetch", "system_info", "process_list",
                 "process_output"}


class _Tail:
    """Uzun cevap akarken ekrana sığan son kısmı göster (Live taşınca satırlar kopyalanmasın)."""

    def __init__(self, text: str):
        self.text = text

    def __rich_console__(self, cons, options):
        from rich.segment import Segment

        lines = cons.render_lines(Markdown(self.text), options, pad=False)
        height = max(6, cons.size.height - 4)
        for line in lines[-height:]:
            yield from line
            yield Segment.line()


class RichUI:
    def __init__(self, verbose: bool = False, model_label=lambda: ""):
        self.verbose = verbose
        self.model_label = model_label
        self._live = None
        self._spin = None
        self._buf = ""

    # ------------------------------------------------------------ yardımcılar
    def _spin_start(self, label: str) -> None:
        self._spin_stop()
        self._spin = console.status(f"[dim]{label}[/]", spinner="dots")
        self._spin.start()

    def _spin_stop(self) -> None:
        if self._spin is not None:
            self._spin.stop()
            self._spin = None

    def _live_stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    # ------------------------------------------------------------------ akış
    def stream_start(self) -> None:
        self._buf = ""
        self._think = ""
        m = self.model_label()
        self._spin_start(f"düşünüyor{' · ' + m if m else ''}...")

    def stream_delta(self, text: str) -> None:
        from rich.live import Live

        self._spin_stop()
        self._buf += text
        if self._live is None:
            self._live = Live(_Tail(self._buf), console=console, refresh_per_second=12, transient=True)
            self._live.start()
        else:
            self._live.update(_Tail(self._buf))

    def thinking_delta(self, text: str) -> None:
        self._think = (getattr(self, "_think", "") + text)[-300:]
        if self._spin is not None and not self._buf:
            last = " ".join(self._think.split())[-70:]
            self._spin.update(f"[dim]düşünüyor… {escape(last)}[/]")

    def stream_reset(self) -> None:
        self._live_stop()
        self._buf = ""
        console.print("[dim]  ↪ model akış ortasında düştü, yedek model baştan yazıyor[/]")
        self._spin_start("yeniden deneniyor...")

    def stream_end(self, text: str, source: str) -> None:
        self._spin_stop()
        self._live_stop()
        if text:
            console.print(Markdown(text))
            if self.verbose and source:
                console.print(f"[dim]  ({source})[/]")

    # ----------------------------------------------------------------- araçlar
    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self._spin_stop()
        if name in COMPACT_TOOLS:
            # Salt okuma araçları (paralel de çalışabilir) bitince tek satır olarak yazılır.
            self._pending_summary = getattr(self, "_pending_summary", {})
            self._pending_summary[call_id] = summary
        else:
            console.print(f"[cyan]●[/] [bold]{name}[/] [dim]{escape(summary)}[/]")
        self._spin_start(f"{name} çalışıyor...")

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        self._spin_stop()
        if name in COMPACT_TOOLS:
            summary = getattr(self, "_pending_summary", {}).pop(call_id, "")
            lines = result.text.strip().splitlines()
            tail = lines[0][:90] if len(lines) == 1 else f"{len(lines)} satır"
            color = "red" if result.is_error else "dim"
            console.print(f"[cyan]●[/] [bold]{name}[/] [dim]{escape(summary)}[/]  [{color}]⎿ {escape(tail)}[/]")
            return
        if result.diff:
            print_diff(result.diff)
            if result.is_error:
                console.print(f"  [red]⎿ {escape(result.text[:300])}[/]")
            return
        lines = result.text.strip().splitlines() or [""]
        style = "red" if result.is_error else "dim"
        out = [f"  ⎿ {escape(lines[0][:200])}"] + [f"    {escape(line[:200])}" for line in lines[1:6]]
        if len(lines) > 6:
            out.append(f"    … +{len(lines) - 6} satır")
        if result.images:
            out.append(f"    (+{len(result.images)} görüntü)")
        console.print(f"[{style}]" + "\n".join(out) + "[/]")

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        self._spin_stop()
        self._live_stop()
        color = "red" if risk == "danger" else ("magenta" if risk == "send" else "yellow")
        console.print(f"[{color}]? İzin gerekiyor[/] [bold]{name}[/] ({risk})\n  {escape(summary)}")
        while True:
            ans = console.input("  [bold][y][/]evet  [bold][n][/]hayır  [bold][a][/]bu araca hep izin ver > ").strip().lower()
            if ans in ("y", "e", "evet", "yes", ""):
                return "y"
            if ans in ("n", "h", "hayır", "no"):
                return "n"
            if ans in ("a", "always", "hep"):
                return "a"

    def info(self, msg: str) -> None:
        self._spin_stop()
        console.print(f"[yellow]{escape(msg)}[/]")


def print_diff(diff: str, max_lines: int = 40) -> None:
    lines = diff.splitlines()
    files = [ln[6:] for ln in lines if ln.startswith("+++ b/")]
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    console.print(f"  [dim]⎿ {escape(files[0] if files else '')}[/] [green]+{added}[/] [red]-{removed}[/]")
    body = [ln for ln in lines if not ln.startswith(("---", "+++"))]
    for ln in body[:max_lines]:
        if ln.startswith("+"):
            console.print(f"    [green on #0f2a17]{escape(ln)}[/]", soft_wrap=True)
        elif ln.startswith("-"):
            console.print(f"    [red on #2d1215]{escape(ln)}[/]", soft_wrap=True)
        elif ln.startswith("@@"):
            console.print(f"    [cyan]{escape(ln)}[/]")
        else:
            console.print(f"    [dim]{escape(ln)}[/]", soft_wrap=True)
    if len(body) > max_lines:
        console.print(f"    [dim]… +{len(body) - max_lines} satır[/]")


def build(cfg: Config, cwd: Path, mode: str, verbose: bool = False) -> tuple[Agent, Router, ToolContext]:
    router = Router(cfg, on_event=(lambda s: console.print(f"[dim]  ↪ {s}[/]")) if verbose else None)
    ui = RichUI(verbose, model_label=lambda: (router.last_ref.get("brain") or router.first_available("brain") or ""))
    decider = Decider.from_config(cfg, router, on_event=lambda s: console.print(f"[dim]  ↪ {s}[/]"))
    ctx = ToolContext(cwd=cwd, config=cfg, router=router, decider=decider,
                      emit=lambda s: console.print(f"[dim]  {s}[/]"))
    agent = Agent(cfg, router, all_tools(cfg, ctx.emit), ctx, Permissions(mode), ui, decider)
    attach_memory(agent, "yazı")
    attach_mcp(agent, ctx, emit=lambda s: console.print(f"[dim]  {s}[/]"))
    return agent, router, ctx


def _check_ready(router: Router) -> bool:
    if router.first_available("brain"):
        return True
    console.print("[red]Kullanılabilir beyin modeli yok.[/] Önce [bold]alice init[/] çalıştır ve "
                  f"{alice_home() / '.env'} dosyasına en az bir API anahtarı ekle (en kolayı GEMINI_API_KEY).")
    return False


# ----------------------------------------------------------------------- REPL
def _ktok(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k"


def status_line(agent: Agent, router: Router, ctx: ToolContext) -> str:
    model = router.last_ref.get("brain") or router.first_available("brain") or "model yok"
    toks = agent.context_tokens()
    req = sum(r["requests"] for r in router.usage.today().values())
    dec = agent.decider.names[0] if agent.decider else "-"
    cwd = str(ctx.cwd)
    if len(cwd) > 40:
        cwd = "…" + cwd[-39:]
    window = router.context_limit(model) if ":" in model else agent.compact_at
    return (f" {model} │ izin: {agent.permissions.mode} │ context {_ktok(toks)}/{_ktok(window)} │ "
            f"bugün {req} istek │ karar: {dec} │ {cwd} ")


def make_prompt_session(agent: Agent, router: Router, ctx: ToolContext):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        buf = event.current_buffer
        # Satır sonu "\" ile bitiyorsa alt satıra geç (her terminalde çalışan yöntem).
        if buf.document.text_before_cursor.endswith("\\"):
            buf.delete_before_cursor(1)
            buf.insert_text("\n")
        else:
            buf.validate_and_handle()

    @kb.add("s-tab")  # izin modunu döndür
    def _(event):
        order = list(MODES)
        agent.permissions.mode = order[(order.index(agent.permissions.mode) + 1) % len(order)]
        event.app.invalidate()

    @kb.add("escape", "enter")  # Alt+Enter
    @kb.add("c-j")              # Ctrl+J
    def _(event):
        event.current_buffer.insert_text("\n")

    alice_home().mkdir(parents=True, exist_ok=True)
    return PromptSession(
        history=FileHistory(str(alice_home() / "history")),
        multiline=True,
        key_bindings=kb,
        prompt_continuation=lambda width, line_number, wrap_count: "  ",
        bottom_toolbar=lambda: status_line(agent, router, ctx),
    )


def repl(agent: Agent, router: Router, ctx: ToolContext, cfg: Config, resume: str | None) -> None:
    from alice import session as sess

    sid = sess.new_session_id()
    agent.on_step = lambda: sess.save(sid, agent.messages, ctx.cwd)
    if resume is not None:
        loaded = sess.load(resume or None)
        if loaded:
            sid, msgs = loaded
            agent.messages = agent.messages[:1] + msgs
            console.print(f"[green]oturum yüklendi:[/] {sid} ({len(msgs)} mesaj)")
        else:
            console.print("[yellow]yüklenecek oturum bulunamadı[/]")

    ps = make_prompt_session(agent, router, ctx)
    console.print(f"[bold magenta]ALICE[/] v{__version__}  [dim]/help komutlar · Enter gönder · Alt+Enter ya da "
                  "satır sonu \\ ile yeni satır · Ctrl+C durdur[/]")
    prefill = ""
    while True:
        try:
            text = ps.prompt("\n› ", default=prefill).strip()
            prefill = ""
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if not text:
            continue
        if text.startswith("/"):
            res = handle_slash(text, agent, router, ctx, cfg)
            if res == "exit":
                break
            if isinstance(res, tuple) and res[0] == "prefill":
                prefill = res[1]
                continue
            if not (isinstance(res, tuple) and res[0] == "run"):
                continue
            text = res[1]
        t0 = time.monotonic()
        agent.run_turn(text)
        console.print(f"[dim]  {time.monotonic() - t0:.1f} sn[/]")
        try:
            sess.save(sid, agent.messages, ctx.cwd)
        except OSError:
            pass
    cleanup(ctx)


def handle_slash(text: str, agent: Agent, router: Router, ctx: ToolContext, cfg: Config):
    """None, "exit", ("run", mesaj) ya da ("prefill", metin) döndürür."""
    from alice import session as sess
    from alice.commands import INIT_PROMPT, council_prompt, expand_custom

    parts = text.split()
    cmd, args = parts[0].lower(), parts[1:]
    if cmd in ("/exit", "/quit", "/q"):
        return "exit"
    if cmd == "/init":
        return ("run", INIT_PROMPT)
    if cmd == "/kurul":
        return ("run", council_prompt(" ".join(args)))
    custom = expand_custom(text, ctx.cwd)
    if custom is not None:
        return ("run", custom)
    if cmd == "/help":
        console.print(HELP)
    elif cmd == "/clear":
        agent.reset()
        console.print("[green]konuşma sıfırlandı[/]")
    elif cmd == "/compact":
        console.print("[green]özetlendi[/]" if agent.compact() else "[yellow]özetlenecek bir şey yok[/]")
    elif cmd in ("/mode", "/plan"):
        if cmd == "/plan":
            agent.permissions.mode = "plan"
        elif args and args[0] in MODES:
            agent.permissions.mode = args[0]
        console.print(f"izin modu: [bold]{agent.permissions.mode}[/]")
    elif cmd == "/rewind":
        return rewind_prompt(agent)
    elif cmd == "/mcp":
        mcp = [t for t in agent.tools.values() if t.name.startswith("mcp__")]
        if not mcp:
            console.print("[dim]MCP sunucusu yok. config.toml'a [mcp.servers.<isim>] ekle (README'de örnek var).[/]")
        for t in mcp:
            console.print(f"[bold]{t.name}[/] [dim]({t.risk})[/] {t.description[:110]}")
    elif cmd == "/memory":
        mem = agent.memory or shared_memory(agent.config)
        console.print(mem.export_markdown() if mem is not None else "[dim](hafıza kapalı)[/]", markup=False)
    elif cmd == "/model":
        if len(args) >= 2:
            role, ref = args[0], args[1]
            if ref == "reset":
                router.role_overrides.pop(role, None)
            else:
                try:
                    parse_ref(ref)
                except ValueError as e:
                    console.print(f"[red]{e}[/]")
                    return None
                router.role_overrides[role] = [ref]
        print_status(router)
    elif cmd == "/think":
        from alice.providers.thinking import LEVELS

        if args and args[-1] in LEVELS:
            role = args[0] if len(args) >= 2 else "*"
            router.thinking_overrides[role] = args[-1]
        roles = ["brain", "fast", "voice", "vision"]
        console.print("düşünme: " + ", ".join(f"{r}={router.thinking_level(r)}" for r in roles)
                      + f"  [dim](kalıcı için config.toml [thinking]; seçenekler: {' | '.join(LEVELS)})[/]")
    elif cmd == "/usage":
        print_usage(router)
    elif cmd == "/tools":
        for t in agent.tools.values():
            console.print(f"[bold]{t.name}[/] [dim]({t.risk})[/] {t.description.splitlines()[0][:110]}")
    elif cmd == "/sessions":
        for sid, cwd, first in sess.list_sessions():
            console.print(f"[bold]{sid}[/] [dim]{cwd}[/]  {first}")
    elif cmd == "/resume":
        loaded = sess.load(args[0] if args else None)
        if loaded:
            agent.messages = agent.messages[:1] + loaded[1]
            console.print(f"[green]yüklendi:[/] {loaded[0]}")
        else:
            console.print("[yellow]oturum yok[/]")
    elif cmd == "/update":
        from alice import updater

        info = updater.check(cfg)
        if info.error and not info.available:
            console.print(f"[yellow]{escape(info.error)}[/]")
        elif not info.available:
            console.print(f"[green]ALICE güncel[/] ({info.branch} @ {info.local[:7]})")
        elif args and args[0] in ("apply", "yap"):
            ok, msg = updater.apply_update(info, cfg, force="--force" in args,
                                           emit=lambda s: console.print(f"[dim]{s}[/]"))
            console.print(("[green]" if ok else "[red]") + escape(msg) + "[/] "
                          + ("[dim](yeni sürüm için ALICE'i kapatıp aç)[/]" if ok else ""))
        else:
            console.print(f"[bold]Yeni sürüm var:[/] {info.local[:7]} → {info.remote[:7]} (CI: {info.ci})")
            for line in info.commits[:8]:
                console.print(f"  [dim]·[/] {escape(line)}")
            console.print("[dim]çekmek için: /update apply[/]")
    elif cmd == "/cwd":
        if args:
            p = ctx.resolve(" ".join(args))
            if p.is_dir():
                ctx.cwd = p
            else:
                console.print(f"[red]klasör yok: {p}[/]")
        console.print(str(ctx.cwd))
    else:
        console.print(f"[red]bilinmeyen komut {cmd}[/] (/help)")
    return None


def rewind_prompt(agent: Agent):
    turns = [(i, m) for i, m in enumerate(agent.messages)
             if m.get("role") == "user" and not m.get("_image") and i > 0
             and not str(m.get("content", "")).startswith("[Önceki konuşmanın özeti]")][-9:]
    if not turns:
        console.print("[yellow]geri sarılacak mesaj yok[/]")
        return None
    for n, (_, m) in enumerate(reversed(turns), 1):
        text = m.get("_display") or (m["content"] if isinstance(m["content"], str) else "(görselli mesaj)")
        console.print(f"  [bold]{n}[/] {escape(text.splitlines()[0][:90])}")
    ans = console.input("Hangi mesajın öncesine dönülsün? (numara, boş = vazgeç) > ").strip()
    if not ans.isdigit() or not 1 <= int(ans) <= len(turns):
        return None
    mi = list(reversed(turns))[int(ans) - 1][0]
    text, restored = agent.rewind(mi)
    console.print("[green]geri sarıldı[/]" + (f", {len(restored)} dosya eski haline getirildi" if restored else ""))
    return ("prefill", text)


def print_status(router: Router) -> None:
    t = Table(title="Rol zincirleri", show_lines=False)
    t.add_column("rol")
    t.add_column("model")
    t.add_column("durum")
    for role, rows in router.status().items():
        for i, (ref, st) in enumerate(rows):
            style = "green" if st == "hazır" else "dim"
            t.add_row(role if i == 0 else "", ref, f"[{style}]{st}[/]")
    console.print(t)


def print_usage(router: Router) -> None:
    t = Table(title="Bugünkü kullanım")
    for c in ("model", "istek", "hata", "girdi tok", "çıktı tok"):
        t.add_column(c)
    for ref, row in sorted(router.usage.today().items()):
        t.add_row(ref, str(row["requests"]), str(row["errors"]), str(row["prompt_tokens"]), str(row["completion_tokens"]))
    console.print(t)


# ------------------------------------------------------------------- doctor
def _doctor_connect(cfg: Config) -> None:
    """Bağlantılar tek satırda: hangileri hazır, hangileri anahtar/izin bekliyor."""
    from alice.connect.manifest import discover
    from alice.connect.runner import ready

    manifests, errors = discover(cfg)
    if not manifests:
        return
    hazır = [m.label for m in manifests.values() if ready(m)[0]]
    bekleyen = [f"{m.label} ({ready(m)[1]})" for m in manifests.values() if not ready(m)[0]]
    console.print(f"[bold]Bağlantılar:[/] " + (f"[green]{', '.join(hazır)}[/]" if hazır else "[dim]hazır yok[/]")
                  + (f"  [dim]bekleyen: {'; '.join(bekleyen)}[/]" if bekleyen else ""))
    for e in errors:
        console.print(f"  [yellow]{escape(e)}[/]")


def _doctor_update(cfg: Config) -> None:
    """Sürüm satırı: hangi commit'tesin, yeni sürüm var mı, otomatik güncelleme hangi modda."""
    from alice import updater

    mode = updater.settings(cfg).get("mode", "auto")
    try:
        info = updater.check(cfg, timeout=10, details=False)
    except Exception as e:  # noqa: BLE001 - doctor hiçbir adımda düşmemeli
        console.print(f"[dim]sürüm kontrolü yapılamadı: {e}[/]")
        return
    if info.error and not info.available:
        console.print(f"[bold]Sürüm:[/] {__version__}  [dim]{info.error}[/]")
    elif info.available:
        console.print(f"[bold]Sürüm:[/] {__version__} @ {info.local[:7]}  "
                      f"[yellow]yeni sürüm var[/] ({info.remote[:7]}) · güncelleme modu: {mode} · "
                      "[bold]alice update[/]")
    else:
        console.print(f"[bold]Sürüm:[/] {__version__} @ {info.local[:7]}  [green]güncel[/] · güncelleme modu: {mode}")


def doctor(cfg: Config) -> int:
    router = Router(cfg, max_wait=0)
    console.print(f"[bold]ALICE home:[/] {alice_home()}  config: {cfg.path or '(varsayılan)'}")
    _doctor_update(cfg)
    _doctor_connect(cfg)
    t = Table(title="Sağlayıcılar")
    for c in ("sağlayıcı", "anahtar", "test modeli", "sonuç"):
        t.add_column(c)
    refs_by_provider: dict[str, str] = {}
    for chain in cfg.roles.values():
        for ref in chain:
            p = ref.split(":", 1)[0]
            refs_by_provider.setdefault(p, ref)
    for name, pc in router.providers_cfg.items():
        key = "gerekmez" if not pc.needs_key else ("var" if pc.api_key else f"[dim]{pc.api_key_env} yok[/]")
        ref = refs_by_provider.get(name, "")
        if not pc.enabled or not ref:
            t.add_row(name, key, ref, "[dim]atlandı[/]")
            continue
        t0 = time.monotonic()
        try:
            res = router.chat(ref, [{"role": "user", "content": "Reply with just: ok"}], max_tokens=50)
            t.add_row(name, key, ref, f"[green]çalışıyor[/] {time.monotonic() - t0:.1f} sn: {res.text.strip()[:20]!r}")
        except AllProvidersFailed as e:
            t.add_row(name, key, ref, f"[red]{e.errors[-1][:110] if e.errors else 'hata'}[/]")
    console.print(t)

    decider = Decider.from_config(cfg, router)
    console.print(f"[bold]Karar motorları:[/] {', '.join(decider.names)}")
    from alice.decider import Choice, DeciderError, Noul

    try:
        d = decider.decide("A login form is visible with username, password fields and a 'Sign in' button. Goal: sign in.",
                           {"next": Choice("Next action?", {"type_user": "Type username", "click_signin": "Click Sign in",
                                                            "scroll": "Scroll down"}),
                            "done": Noul("Is the user already signed in?")})
        console.print(f"  test kararı: next={d['next'].value} ({d['next'].confidence:.2f}), done={d['done'].value:.2f} "
                      f"[dim]{d.latency * 1000:.0f} ms, {d.engine}[/]")
    except DeciderError as e:
        console.print(f"  [red]karar motoru çalışmıyor: {e}[/]")

    console.print("[bold]Opsiyonel bileşenler:[/]")
    for mod, why in (("mss", "ekran görüntüsü"), ("pyautogui", "fare/klavye"), ("pyperclip", "pano, Türkçe yazma"),
                     ("pygetwindow", "pencere yönetimi (Windows/macOS)"), ("pywinauto", "UI Automation (Windows)"),
                     ("playwright", "tarayıcı")):
        ok = importlib.util.find_spec(mod) is not None
        console.print(f"  {'[green]✓[/]' if ok else '[red]✗[/]'} {mod:<12} {why}")
    return 0


# ------------------------------------------------------------------- models
def models_cmd(cfg: Config, refresh: bool, write: bool) -> int:
    """Ücretsiz modelleri canlı ölç ve sırala (sonuç ~/.alice/models.json, "auto" zincirleri bunu kullanır)."""
    from alice.providers import ranking

    router = Router(cfg)
    cache = ranking.load_cache()
    if refresh or not cache:
        with console.status("modeller ölçülüyor (her biri gerçek bir araç çağrısıyla, ~1-2 dk)...") as st:
            cache = ranking.refresh(router, emit=lambda s: st.update(f"modeller ölçülüyor... {s}"))
        router.reload_auto(cache)
    t = Table(title=f"Ücretsiz modeller (ölçüm: {cache.get('probed_at', '?')})")
    for c in ("model", "durum", "kalite", "süre sn", "ilk token sn", "token", "token/sn"):
        t.add_column(c, justify="left" if c in ("model", "durum") else "right")
    for r in cache.get("results", {}).values():
        state = "[green]çalışıyor[/]" if r["ok"] else f"[red]{r['error_kind']}[/]"
        t.add_row(r["ref"], state, str(r["quality"]), f"{r['latency']:.1f}", f"{r['ttft']:.1f}",
                  str(r["tokens"]), f"{r['tps']:.0f}")
    console.print(t)
    for p, e in (cache.get("catalog_errors") or {}).items():
        console.print(f"[dim]{p}: {e}[/]")
    console.print("[bold]Otomatik sıra (\"auto\"):[/]")
    for role, refs in (cache.get("chains") or {}).items():
        console.print(f"  {role}: {', '.join(refs) or '-'}")
    console.print("[bold]Şu anki zincirler:[/]")
    for role in ("brain", "fast", "vision"):
        console.print(f"  {role}: {', '.join(router.chain(role))}")
    if write:
        path = write_roles({"brain": ["auto", "ollama:qwen3:8b"], "fast": ["auto", "ollama:qwen3:8b"],
                            "vision": ["auto", "gemini:gemini-flash-lite-latest"]})
        console.print(f"[green]config rolleri \"auto\" yapıldı:[/] {path}")
    elif any("auto" not in (cfg.roles.get(r) or []) for r in ("brain", "fast", "vision")):
        console.print("[dim]Zincirlerin tamamen ölçüme göre kurulması için: alice models --write[/]")
    return 0


def write_roles(chains: dict[str, list[str]]) -> Path:
    """config.toml'daki ilgili rol satırlarını değiştir (boş öneri olan rollere dokunma)."""
    init_home()
    path = alice_home() / "config.toml"
    text = path.read_text(encoding="utf-8")
    for role, refs in chains.items():
        if not refs:
            continue
        block = f"{role} = [" + ", ".join(f'"{r}"' for r in refs) + "]"
        rx = re.compile(rf"(?ms)^{role}\s*=\s*\[.*?\]")
        if rx.search(text):
            text = rx.sub(lambda _m, b=block: b, text, count=1)
        else:
            text = text.replace("[roles]\n", f"[roles]\n{block}\n", 1)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------- reflex
def reflex_cmd(cfg: Config, a: argparse.Namespace) -> int:
    from alice.reflex.loop import ReflexLoop, make_text_writer
    from alice.reflex.observers import BrowserObserver, DesktopObserver

    router = Router(cfg)
    decider = Decider.from_config(cfg, router, on_event=lambda s: console.print(f"[dim]{s}[/]"))
    ctx = ToolContext(cwd=Path.cwd(), config=cfg, router=router, decider=decider)
    inputs = dict(kv.split("=", 1) for kv in a.input or [])
    try:
        observer = BrowserObserver(ctx, a.url) if a.target == "browser" else DesktopObserver(ctx, a.title)
        loop = ReflexLoop(observer, decider, make_text_writer(router),
                          min_confidence=float(cfg.decider.get("min_confidence", 0.55)),
                          emit=lambda s: console.print(f"[cyan]{s}[/]"))
        res = loop.run(a.goal, max_steps=a.max_steps, inputs=inputs)
        console.print(res.report())
        if a.target == "browser" and not a.close:
            console.input("[dim]tarayıcıyı kapatmak için Enter[/]")
    finally:
        cleanup(ctx)
    return 0 if res.status == "done" else 1


# ------------------------------------------------------------------- update
def update_cmd(argv: list[str]) -> int:
    """alice update [--check] [--force] [--restart]"""
    from alice import updater

    ap = argparse.ArgumentParser(prog="alice update", description="ALICE'i GitHub'daki son sürüme çek")
    ap.add_argument("--check", action="store_true", help="sadece bak, güncelleme yapma")
    ap.add_argument("--force", action="store_true", help="kirli ağaç / kırmızı CI uyarısını geç")
    ap.add_argument("--restart", action="store_true", help="güncelleme sonrası uygulamayı aç")
    a = ap.parse_args(argv)

    cfg = load_config()
    info = updater.check(cfg)
    if info.error and not info.available:
        console.print(f"[yellow]{info.error}[/]")
        return 1
    if not info.available:
        console.print(f"[green]ALICE güncel[/] ({info.branch} @ {info.local[:7]})")
        return 0
    console.print(f"[bold]Yeni sürüm var:[/] {info.local[:7]} → {info.remote[:7]} "
                  f"({info.branch}, CI: {info.ci})")
    for line in info.commits[:10]:
        console.print(f"  [dim]·[/] {escape(line)}")
    if len(info.commits) > 10:
        console.print(f"  [dim]… ve {len(info.commits) - 10} commit daha[/]")
    if a.check:
        if info.blocked:
            console.print(f"[yellow]Otomatik güncellenmez: {info.blocked}[/]")
        return 0
    ok, msg = updater.apply_update(info, cfg, force=a.force, emit=lambda s: console.print(f"[dim]{s}[/]"))
    console.print(("[green]" if ok else "[red]") + escape(msg) + "[/]")
    if ok and a.restart:
        console.print("[dim]ALICE yeniden açılıyor…[/]")
        updater.restart([sys.executable, "-m", "alice", "app"])
    return 0 if ok else 1


# ---------------------------------------------------------------------- app
def default_workspace() -> Path:
    """Uygulamanın başlangıç klasörü. Ev klasörü çok büyük ("bu proje" deyince her yeri tarıyordu), bu yüzden:
    config'teki [app] workspace > son kullanılan klasör > Belgeler\\ALICE (yoksa oluşturulur)."""
    try:
        ws = (load_config().data.get("app", {}) or {}).get("workspace", "")
    except Exception:  # noqa: BLE001
        ws = ""
    if ws and Path(ws).expanduser().is_dir():
        return Path(ws).expanduser()
    try:
        last = Path((alice_home() / "last_cwd").read_text(encoding="utf-8").strip())
        if last.is_dir():
            return last
    except OSError:
        pass
    docs = Path.home() / "Documents"
    ws_dir = (docs if docs.is_dir() else Path.home()) / "ALICE"
    try:
        ws_dir.mkdir(parents=True, exist_ok=True)
        return ws_dir
    except OSError:
        return Path.home()


def app_cmd(argv: list[str]) -> int:
    from alice.app.server import AppServer, AppState
    from alice.app.window import open_window

    ap = argparse.ArgumentParser(prog="alice app")
    ap.add_argument("--cwd", type=Path, help="başlangıç çalışma klasörü (varsayılan: ev klasörün)")
    ap.add_argument("--port", type=int, default=0, help="sabit port (varsayılan: rastgele)")
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--window", choices=["auto", "webview", "app", "browser", "none"], default="auto",
                    help="none: pencere açma, sadece adresi yaz")
    a = ap.parse_args(argv)
    cwd = (a.cwd or default_workspace()).resolve()
    cfg = load_config(cwd=cwd)
    # Açılışta güncelleme: [update] mode="auto" ise yeni commit çekilir ve ALICE kendini yeniden başlatır
    # (bu çağrı o durumda geri dönmez). mode="notify" ise sadece bilgi döner, arayüzde duyurulur.
    from alice import updater

    pending = updater.startup_update(cfg, emit=lambda s: console.print(f"[dim]güncelleme: {s}[/]"))
    updater.mark_running()  # bundan sonra diske yeni kod gelirse "yeniden başlat" deriz
    updater.mark_app()      # çekirdek, uygulama açıkken kendi başına çekmesin
    state = AppState(cfg, cwd, a.mode or cfg.agent.get("permission_mode", "auto"))
    state.watch_updates(pending)
    ann = updater.consume_announcement("app")
    if ann and ann.get("changes"):
        lines = "\n".join(f"- {c}" for c in ann["changes"][:8])
        more = len(ann["changes"]) - 8
        state.ui.info(f"ALICE güncellendi ({ann['from'][:7]} → {ann['to'][:7]}). Neler değişti:\n{lines}"
                      + (f"\n… ve {more} değişiklik daha" if more > 0 else ""))
    server = AppServer(state, a.port).start()
    if cfg.data.get("voice", {}).get("enabled"):
        # "Hey Alice" çekirdekte dinlenir; uygulama açılınca çekirdek kapalıysa onu da başlat.
        try:
            from alice.core.service import start_detached

            if start_detached():
                state.ui.info("Sesli asistan için arka plandaki ALICE çekirdeği başlatıldı.")
        except Exception:  # noqa: BLE001 - ses başlamazsa uygulama yine açılsın
            pass
    console.print(f"[bold magenta]ALICE[/] uygulaması: {server.url}")
    if not state.router.first_available("brain"):
        state.ui.info(f"Kullanılabilir model yok. {alice_home() / '.env'} dosyasına bir API anahtarı ekleyip uygulamayı yeniden aç.")
    try:
        if a.window == "none":
            console.print("[dim]Durdurmak için Ctrl+C[/]")
            while True:
                time.sleep(3600)
        else:
            open_window(server.url, prefer=a.window)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        updater.clear_app()
    return 0


def shortcut_cmd() -> int:
    from alice.app.window import create_shortcut

    init_home()
    for lnk in create_shortcut():
        console.print(f"[green]kısayol oluşturuldu:[/] {lnk}")
    return 0


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.stdout is None or sys.stderr is None:
        # pythonw.exe (konsolsuz kısayol) altında stdout yok; çıktıyı yut.
        import os

        from alice.config import alice_home

        devnull = open(os.devnull, "w", encoding="utf-8")
        try:  # hatalar kaybolmasın: ~/.alice/core.log (2 MB'ı geçince baştan)
            log = alice_home() / "core.log"
            if log.exists() and log.stat().st_size > 2_000_000:
                log.replace(log.with_suffix(".log.1"))
            err = open(log, "a", encoding="utf-8", buffering=1)
        except OSError:
            err = devnull
        sys.stdout = sys.stdout or devnull
        sys.stderr = sys.stderr or err
        console.file = sys.stdout
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    if argv and argv[0] == "init":
        for p in init_home(force="--force" in argv):
            console.print(f"[green]oluşturuldu:[/] {p}")
        console.print(f"Şimdi {alice_home() / '.env'} dosyasına API anahtarlarını yaz, sonra [bold]alice doctor[/].")
        return 0
    if argv and argv[0] == "connect":
        from alice.connect.cli import connect_cmd

        return connect_cmd(argv[1:])
    if argv and argv[0] == "update":
        return update_cmd(argv[1:])
    if argv and argv[0] == "doctor":
        return doctor(load_config())
    if argv and argv[0] == "models":
        return models_cmd(load_config(), "--refresh" in argv, "--write" in argv)
    if argv and argv[0] == "app":
        try:
            return app_cmd(argv[1:])
        except Exception:
            # Kısayoldan (konsolsuz) açılınca hata görünmez; dosyaya yaz.
            from alice.app.server import log_error

            log_error()
            raise
    if argv and argv[0] in ("core", "rules", "tasks", "feeds"):
        from alice.core import cli as core_cli

        return {"core": core_cli.core_cmd, "rules": core_cli.rules_cmd, "tasks": core_cli.tasks_cmd,
                "feeds": core_cli.feeds_cmd}[argv[0]](argv[1:])
    if argv and argv[0] in ("notes", "brief", "report"):
        from alice.extras import cli as extras_cli

        return getattr(extras_cli, f"{argv[0]}_cmd")(argv[1:])
    if argv and argv[0] in ("watch", "phone"):
        from alice.app import phone

        return (phone.watch_cmd if argv[0] == "watch" else phone.phone_cmd)(argv[1:])
    if argv and argv[0] == "dcs":
        from alice.sims.dcs import dcs_cmd

        return dcs_cmd(argv[1:])
    if argv and argv[0] == "quick":
        from alice.app.quick import quick_cmd

        return quick_cmd(argv[1:])
    if argv and argv[0] == "voice":
        from alice.voice.cli import voice_cmd

        return voice_cmd(argv[1:])
    if argv and argv[0] == "shortcut":
        return shortcut_cmd()
    if argv and argv[0] == "plugins":
        from alice.plugins import plugins_cmd

        return plugins_cmd(argv[1:])
    if argv and argv[0] == "reflex":
        rp = argparse.ArgumentParser(prog="alice reflex")
        rp.add_argument("goal")
        rp.add_argument("--target", choices=["browser", "desktop"], default="browser")
        rp.add_argument("--url")
        rp.add_argument("--title", help="desktop: pencere başlığı parçası")
        rp.add_argument("--input", action="append", help="alan=metin (tekrarlanabilir)")
        rp.add_argument("--max-steps", type=int, default=25)
        rp.add_argument("--close", action="store_true", help="bitince tarayıcıyı hemen kapat")
        return reflex_cmd(load_config(), rp.parse_args(argv[1:]))

    ap = argparse.ArgumentParser(prog="alice", description="ALICE ajan harness")
    ap.add_argument("prompt", nargs="*", help="ilk mesaj (verilirse REPL bu mesajla başlar)")
    ap.add_argument("-p", "--print", dest="oneshot", action="store_true", help="tek seferlik çalıştır ve çık")
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--yolo", action="store_true", help="hiçbir izin sorma (tam erişim)")
    ap.add_argument("--cwd", type=Path)
    ap.add_argument("--model", help="beyin rolünü tek modele sabitle, ör. gemini:gemini-flash-latest")
    ap.add_argument("--resume", nargs="?", const="", help="son (ya da verilen id'li) oturumu devam ettir")
    ap.add_argument("-v", "--verbose", action="store_true", help="hangi modelin cevap verdiğini göster")
    ap.add_argument("--version", action="version", version=f"alice {__version__}")
    a = ap.parse_args(argv)

    cwd = (a.cwd or Path.cwd()).resolve()
    cfg = load_config(cwd=cwd)
    mode = "yolo" if a.yolo else (a.mode or cfg.agent.get("permission_mode", "auto"))
    agent, router, ctx = build(cfg, cwd, mode, a.verbose)
    if a.model:
        router.role_overrides["brain"] = [a.model]
    if not _check_ready(router):
        return 2
    prompt = " ".join(a.prompt).strip()
    if a.oneshot:
        if not prompt:
            prompt = sys.stdin.read().strip()
        try:
            out = agent.run_turn(prompt)
        finally:
            cleanup(ctx)
        return 0 if out else 1
    if prompt:
        agent.run_turn(prompt)
    repl(agent, router, ctx, cfg, a.resume)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
