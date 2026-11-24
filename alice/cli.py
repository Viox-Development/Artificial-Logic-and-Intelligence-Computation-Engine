"""ALICE command line.

  alice                      interactive session (REPL)
  alice "task"               start REPL with this message
  alice -p "task"            run once, print result, exit (--output-format json|stream-json: for scripts)
  alice init [--force]       create config.toml, .env, and personality.md under ~/.alice
  alice eval [--add|--from-log N]   test set from real conversations: fixed or broken
  alice setup google         Google Calendar + Gmail setup wizard (step-by-step, opens pages on its own)
  alice doctor               test keys, models, and optional dependencies
  alice models [--refresh] [--write]  benchmark/rank free models live; --write sets roles to auto
  alice reflex "goal"        run Jev/System One reflex loop standalone
  alice app                  open desktop application
  alice update [--check]     update to latest release on GitHub (pulls what is merged into main)
  alice connect [name]       service connections (Spotify, Steam, Discord, YouTube, custom APIs)
  alice feeds                feeds that watch connected services and notify when something new happens
  alice log [n]              what ALICE sent on your behalf (message, DM, email) and rejections
  alice shortcut             place ALICE shortcut on Desktop and Start menu (Windows)
  alice core run|install|uninstall|status|mode|stats   background core (monitoring, modes, tasks)
  alice rules [add "phrase"] alert rules ("tell me if GPU overheats for a long time")
  alice tasks [add "phrase"] scheduled tasks ("back up my projects every Sunday night")
  alice voice [audition|use|library|say|test|status|models|install-local|on|off]   voice assistant and voice selection
  alice quick [--open]       quick bar shortcuts (Ctrl+Alt+A / S / C)
  alice dcs install|uninstall|status|kneeboard   DCS connection
  alice watch [status|import <db>]     smartwatch data and sync frequency
  alice phone [--new-token]  phone bridge (ALICE from phone, live pulse)
  alice notes | brief | report         notes, morning brief, weekly computer report
  alice plugins [new <name>] list plugins / create new plugin template

For details on each command: alice <command> --help
"""
from __future__ import annotations

import argparse
import importlib.util
import json
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
from alice.config import Config, alice_home, init_home, load_config, refresh_home
from alice.decider import Decider
from alice.memory import attach_memory, shared_memory
from alice.permissions import MODES, Permissions
from alice.providers.router import Router, parse_ref
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
  /think [rol] <seviye>       düşünme seviyesi: off | low | medium | high | default, ör. /think brain off
  /kurul <konu>               konuyu inceleme kuruluna sor (eleştirmen, savunucu, kontrolcü, uzmanlar)
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
    """Show the last part that fits the screen while long responses stream (avoid line duplication when Live overflows)."""

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

    # ---------------------------------------------------------------- helpers
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

    # ----------------------------------------------------------------- stream
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

    # ------------------------------------------------------------------ tools
    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self._spin_stop()
        if name in COMPACT_TOOLS:
            # Read-only tools (which can also run in parallel) are printed as a single line when done.
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
            ans = console.input("  [bold][y][/]evet  [bold][n][/]hayır  [bold][a][/]bu oturumda hep  "
                                "[bold][p][/]bu projede hep (kalıcı) > ").strip().lower()
            if ans in ("y", "e", "evet", "yes", ""):
                return "y"
            if ans in ("n", "h", "hayır", "no"):
                return "n"
            if ans in ("a", "always", "hep"):
                return "a"
            if ans in ("p", "proje", "kalıcı"):
                return "p"

    def ask_choice(self, question: str, options: list[str], multi: bool = False) -> str:
        self._spin_stop()
        self._live_stop()
        console.print(f"[cyan]? {escape(question)}[/]")
        for i, o in enumerate(options, 1):
            console.print(f"  [bold]{i}[/] {escape(o)}")
        hint = "numaralar virgülle" if multi else "numara"
        ans = console.input(f"  {hint} ya da kendi cevabın (boş = vazgeç) > ").strip()
        picks = [p.strip() for p in ans.split(",")] if ans else []
        if picks and all(p.isdigit() and 1 <= int(p) <= len(options) for p in picks):
            chosen = [options[int(p) - 1] for p in (picks if multi else picks[:1])]
            return ", ".join(chosen)
        return ans

    def info(self, msg: str) -> None:
        self._spin_stop()
        console.print(f"[yellow]{escape(msg)}[/]")


class JsonUI:
    """`alice -p --output-format json|stream-json`: machine-readable output for scripts and automations. Only JSON
    goes to stdout (stream-json: one per line); tools requiring permission are rejected without asking (except yolo)."""

    def __init__(self, out, stream: bool = False):
        self.out = out
        self.stream = stream
        self.models: list[str] = []
        self.tools: list[str] = []
        self.errors: list[str] = []

    def emit(self, ev: dict) -> None:
        if self.stream:
            self.out.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self.out.flush()

    def stream_start(self) -> None:
        pass

    def stream_delta(self, text: str) -> None:
        self.emit({"type": "delta", "text": text})

    def stream_reset(self) -> None:
        self.emit({"type": "reset"})

    def stream_end(self, text: str, source: str) -> None:
        if source and source not in self.models:
            self.models.append(source)
        if text or source:
            self.emit({"type": "assistant", "text": text, "model": source})

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self.tools.append(name)
        self.emit({"type": "tool_use", "id": call_id, "name": name, "summary": summary})

    def tool_end(self, call_id: str, name: str, result) -> None:
        self.emit({"type": "tool_result", "id": call_id, "name": name, "is_error": bool(result.is_error),
                   "text": result.text[:4000]})

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        self.emit({"type": "permission_denied", "name": name, "summary": summary, "risk": risk})
        return "n"

    def info(self, msg: str) -> None:
        self.errors.append(msg)
        self.emit({"type": "info", "text": msg})


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
        if not any(pc.needs_key and pc.api_key for pc in router.providers_cfg.values()):
            # The only keyless option is local Ollama; often not installed, tell what to do instead of an error list.
            console.print(f"[yellow]Hiç API anahtarı yok, sadece yerel Ollama denenecek.[/] {alice_home() / '.env'} "
                          "dosyasına en az bir anahtar ekle (en kolayı GEMINI_API_KEY), sonra [bold]alice doctor[/].")
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
        # If line ends with "\", move to next line (method that works across all terminals).
        if buf.document.text_before_cursor.endswith("\\"):
            buf.delete_before_cursor(1)
            buf.insert_text("\n")
        else:
            buf.validate_and_handle()

    @kb.add("s-tab")  # cycle permission mode
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
            pass  # the session is saved again after the next message
    cleanup(ctx)


def handle_slash(text: str, agent: Agent, router: Router, ctx: ToolContext, cfg: Config):
    """Returns None, "exit", ("run", message), or ("prefill", text)."""
    from alice import session as sess
    from alice.commands import builtin_prompt, expand_custom

    parts = text.split()
    cmd, args = parts[0].lower(), parts[1:]
    if cmd in ("/exit", "/quit", "/q"):
        return "exit"
    custom = expand_custom(text, ctx.cwd)
    if custom is not None:
        return ("run", custom)
    prompt = builtin_prompt(text)
    if prompt is not None:
        return ("run", prompt)
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
        elif args:
            console.print(f"[red]bilinmeyen mod: {escape(args[0])}[/] (seçenekler: {' | '.join(MODES)})")
        console.print(f"izin modu: [bold]{agent.permissions.mode}[/]")
    elif cmd == "/rewind":
        return rewind_prompt(agent)
    elif cmd == "/mcp":
        mcp = [t for t in agent.tools.values() if t.name.startswith("mcp__")]
        if not mcp:
            console.print("[dim]MCP sunucusu yok. config.toml'a " + escape("[mcp.servers.<isim>]")
                          + " ekle (README'de örnek var).[/]")
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
             if m.get("role") == "user" and not m.get("_image") and not m.get("_nudge") and i > 0
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
    """Connections in a single line: which are ready, which are waiting for keys/permissions."""
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
    """Version line: which commit you are on, whether a new version is available, auto-update mode."""
    from alice import updater

    mode = updater.settings(cfg).get("mode", "auto")
    try:
        info = updater.check(cfg, timeout=10, details=False)
    except Exception as e:  # noqa: BLE001 - doctor should never crash at any step
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
    for name, why in cfg.provider_errors().items():
        console.print(f"[yellow]{escape(f'[providers.{name}] kullanılmıyor: {why}')}[/]")
    _doctor_update(cfg)
    _doctor_connect(cfg)
    from alice.doctor import probe_providers, run_all

    t = Table(title="Sağlayıcılar")
    for c in ("sağlayıcı", "anahtar", "test modeli", "sonuç"):
        t.add_column(c)
    color = {"ok": "green", "fail": "red"}
    for row in probe_providers(cfg):
        detail = ("çalışıyor " if row["status"] == "ok" else "") + row["detail"]
        key = row["key"] if row["key"] != "yok" else f"[dim]{row['env']} yok[/]"
        c = color.get(row["status"])
        t.add_row(row["name"], key, row["model"], f"[{c}]{escape(detail)}[/]" if c else f"[dim]{escape(detail)}[/]")
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

    marks = {"ok": "[green]✓[/]", "warn": "[yellow]![/]", "off": "[dim]–[/]", "fail": "[red]✗[/]"}
    console.print("[bold]Özellikler:[/]")
    for c in run_all(cfg, Path.cwd()):
        console.print(f"  {marks.get(c.status, '?')} {c.name:<24} {escape(c.detail)}"
                      + (f"  [dim]→ {escape(c.fix)}[/]" if c.fix and c.status != "ok" else ""))

    console.print("[bold]Opsiyonel bileşenler:[/]")
    for mod, why in (("mss", "ekran görüntüsü"), ("pyautogui", "fare/klavye"), ("pyperclip", "pano, Türkçe yazma"),
                     ("pygetwindow", "pencere yönetimi (Windows/macOS)"), ("pywinauto", "UI Automation (Windows)"),
                     ("playwright", "tarayıcı"), ("webview", "uygulama penceresi"), ("pystray", "tepsi simgesi"),
                     ("sounddevice", "mikrofon ve ses çalma"), ("pynput", "bas-konuş ve hızlı bar kısayolları")):
        ok = importlib.util.find_spec(mod) is not None
        hint = "" if ok else f"  [dim]{escape(install_hint(mod))}[/]"
        console.print(f"  {'[green]✓[/]' if ok else '[red]✗[/]'} {mod:<12} {why}{hint}")
    return 0


# ------------------------------------------------------------------- models
def models_cmd(cfg: Config, refresh: bool, write: bool) -> int:
    """Benchmark and rank free models live (result in ~/.alice/models.json, "auto" chains use this)."""
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
    """Update corresponding role lines in config.toml (do not touch roles with empty recommendations)."""
    # Writes only to the [roles] section (does not touch keys with same name in other sections); works with
    # "[roles]  # comment" headers and missing roles. Previously searched plain text: new role was silently omitted.
    from alice.config import set_config_value

    init_home()
    path = alice_home() / "config.toml"
    for role, refs in chains.items():
        if refs:
            path = set_config_value("roles", role, list(refs))
    return path


# ------------------------------------------------------------------- reflex
def reflex_cmd(cfg: Config, a: argparse.Namespace) -> int:
    from alice.reflex.loop import ReflexLoop, make_text_writer
    from alice.reflex.observers import BrowserObserver, DesktopObserver
    from alice.tools.base import ToolError
    from alice.tools.browser import NO_SEPARATE

    bad = [kv for kv in a.input or [] if "=" not in kv]
    if bad:
        console.print(f"[red]--input alan=metin biçiminde olmalı:[/] {escape(bad[0])}")
        return 2
    inputs = dict(kv.split("=", 1) for kv in a.input or [])
    router = Router(cfg)
    decider = Decider.from_config(cfg, router, on_event=lambda s: console.print(f"[dim]{s}[/]"))
    ctx = ToolContext(cwd=Path.cwd(), config=cfg, router=router, decider=decider)
    try:
        observer = BrowserObserver(ctx, a.url) if a.target == "browser" else DesktopObserver(ctx, a.title)
        loop = ReflexLoop(observer, decider, make_text_writer(router),
                          min_confidence=float(cfg.decider.get("min_confidence", 0.55)),
                          emit=lambda s: console.print(f"[cyan]{s}[/]"))
        res = loop.run(a.goal, max_steps=a.max_steps, inputs=inputs)
        console.print(res.report())
        if a.target == "browser" and not a.close:
            console.input("[dim]tarayıcıyı kapatmak için Enter[/]")
    except ToolError as e:
        if str(e) == NO_SEPARATE:  # text written for the model; tell what to do on the command line
            console.print("[red]Açık tarayıcına bağlanılamadı ve ayrı pencere açmak kapalı.[/] Tarayıcını "
                          "--remote-debugging-port=9222 ile aç ya da config.toml " + escape("[agent]")
                          + " bölümüne browser_allow_separate = true yaz.")
        else:
            console.print(f"[red]{escape(str(e))}[/]")
        return 1
    finally:
        cleanup(ctx)
    return 0 if res.status in ("done", "clicked", "likely_done", "steps_done") else 1


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
    """Application starting directory. Home folder is too large (scanned everywhere when saying "this project"), so:
    [app] workspace in config > last used directory > Documents\\ALICE (created if missing)."""
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
        pass  # no saved folder: fall back to the current one
    docs = Path.home() / "Documents"
    ws_dir = (docs if docs.is_dir() else Path.home()) / "ALICE"
    try:
        ws_dir.mkdir(parents=True, exist_ok=True)
        return ws_dir
    except OSError:
        return Path.home()


def _attach_to_core() -> int | None:
    """If app is open in core, open window to it (starting core if needed). Returns None if unavailable: app is set up
    in its own process as before. Closing window does not stop core; window closes when core stops."""
    from alice.app import host
    from alice.app.window import open_window

    try:
        cfg = load_config()
        if not (cfg.data.get("app", {}) or {}).get("in_core", True):
            return None
        url = host.attach()
    except Exception:  # noqa: BLE001 - if connection fails, open using the legacy path
        return None
    if not url:
        return None
    console.print(f"[bold magenta]ALICE[/] uygulaması (çekirdekte): {url.split('?')[0]}")
    try:
        open_window(url, watch=host.follow(url))
    except KeyboardInterrupt:
        pass  # Ctrl+C closes the window
    return 0


def app_cmd(argv: list[str]) -> int:
    from alice.app.server import AppServer, AppState
    from alice.app.window import open_window

    ap = TrArgumentParser(prog="alice app", description="Masaüstü uygulamasını aç.")
    ap.add_argument("--cwd", type=Path, help="başlangıç çalışma klasörü (varsayılan: config [app] workspace, "
                    "son kullanılan klasör ya da Belgeler/ALICE)")
    ap.add_argument("--port", type=int, default=0, help="sabit port (varsayılan: rastgele)")
    ap.add_argument("--mode", choices=MODES, help="izin modu (varsayılan: config permission_mode)")
    ap.add_argument("--window", choices=["auto", "webview", "app", "browser", "none"], default="auto",
                    help="pencere türü: auto (pywebview > Edge/Chrome uygulama modu > tarayıcı); none: pencere açma, sadece adresi yaz")
    ap.add_argument("--standalone", action="store_true",
                    help="çekirdeğe bağlanma, uygulamayı bu süreçte kur (eski davranış)")
    a = ap.parse_args(argv)
    if not (a.standalone or a.cwd or a.port or a.mode or a.window == "none"):
        attached = _attach_to_core()
        if attached is not None:
            return attached
    cwd = (a.cwd or default_workspace()).resolve()
    cfg = load_config(cwd=cwd)
    # Update on startup: if [update] mode="auto", new commit is pulled and ALICE restarts itself
    # (this call does not return in that case). If mode="notify", only info is returned, announced in UI.
    from alice import updater

    if updater.boot_check() == "rolled":  # new version crashed repeatedly: reverted to previous version, open with it
        console.print("[yellow]yeni sürüm açılamadı, önceki sürüme dönüldü; yeniden başlatılıyor[/]")
        updater.restart(env={"ALICE_RESTARTED": "1"})
    pending = updater.startup_update(cfg, emit=lambda s: console.print(f"[dim]güncelleme: {s}[/]"))
    updater.mark_running()  # if new code hits the disk after this, we suggest "restart"
    updater.watch_health()  # if we stay up for 90s, the new version is healthy
    updater.mark_app()      # prevent core from pulling on its own while app is open
    state = AppState(cfg, cwd, a.mode or cfg.agent.get("permission_mode", "auto"))
    state.watch_updates(pending)
    added = refresh_home()  # key lines for new providers from the update are added to .env
    if added:
        state.ui.info(f"Yeni anahtar alanları geldi: {', '.join(added)}. Bağlantılar → Anahtarlar'dan girebilirsin.")
    rb = updater.consume_rollback("app")
    if rb:
        state.ui.info(f"Son güncelleme ({str(rb.get('from'))[:7]}) açılamadığı için önceki sürüme "
                      f"({str(rb.get('to'))[:7]}) dönüldü. Sebep: {rb.get('reason')}. Düzeltme gelince otomatik "
                      "güncellenecek.")
    ann = updater.consume_announcement("app")
    if ann and ann.get("changes"):
        lines = "\n".join(f"- {c}" for c in ann["changes"][:8])
        more = len(ann["changes"]) - 8
        state.ui.info(f"ALICE güncellendi ({ann['from'][:7]} → {ann['to'][:7]}). Neler değişti:\n{lines}"
                      + (f"\n… ve {more} değişiklik daha" if more > 0 else ""))
    server = AppServer(state, a.port).start()
    if cfg.data.get("voice", {}).get("enabled"):
        # "Hey Alice" is listened for in core; if core is stopped when app opens, start it too.
        try:
            from alice.core.service import start_detached

            if start_detached():
                state.ui.info("Sesli asistan için arka plandaki ALICE çekirdeği başlatıldı.")
        except Exception:  # noqa: BLE001 - if voice fails to start, app should still open
            pass
    console.print(f"[bold magenta]ALICE[/] uygulaması: {server.url}")
    if not state.router.first_available("brain"):
        state.ui.info("Kullanılabilir model yok. Bağlantılar → Anahtarlar'dan bir API anahtarı gir (ör. Gemini, ücretsiz); "
                      "yeniden başlatmak gerekmez.")
    try:
        if a.window == "none":
            console.print("[dim]Durdurmak için Ctrl+C[/]")
            while True:
                time.sleep(3600)
        else:
            open_window(server.url, prefer=a.window)
    except KeyboardInterrupt:
        pass  # Ctrl+C stops the app
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


# ------------------------------------------------------------ argparse (Turkish)
_ARGPARSE_TR = (
    ("unrecognized arguments", "tanınmayan argüman"),
    ("the following arguments are required", "şu argümanlar gerekli"),
    ("invalid choice", "geçersiz seçim"),
    ("choose from", "seçenekler:"),
    ("argument ", "argüman "),
    ("expected one argument", "bir değer bekleniyor"),
    ("invalid int value", "geçersiz sayı"),
    ("invalid float value", "geçersiz sayı"),
    ("ambiguous option", "belirsiz seçenek"),
    ("could match", "şunlardan biri olabilir"),
)


class _TrHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, "kullanım: " if prefix is None else prefix)


class TrArgumentParser(argparse.ArgumentParser):
    """Argparse with Turkish help headings and error messages (keep CLI language consistent)."""

    def __init__(self, *args, **kw):
        kw.setdefault("formatter_class", _TrHelpFormatter)
        kw["add_help"] = False
        super().__init__(*args, **kw)
        self._positionals.title = "argümanlar"
        self._optionals.title = "seçenekler"
        self.add_argument("-h", "--help", action="help", help="bu yardımı göster ve çık")

    def error(self, message: str):  # type: ignore[override]
        for en, tr in _ARGPARSE_TR:
            message = message.replace(en, tr)
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: hata: {message} (yardım: {self.prog} --help)\n")


def package_version() -> str:
    """Installed package version; if not installed (running from repo folder), version in code."""
    try:
        from importlib.metadata import version

        return version("alice-harness")
    except Exception:  # noqa: BLE001 - PackageNotFoundError or corrupted metadata
        return __version__


# -------------------------------------------------------------- subcommands
SUBCOMMANDS = ("init", "doctor", "models", "reflex", "app", "shortcut", "core", "rules", "tasks", "voice", "quick",
               "dcs", "watch", "phone", "notes", "brief", "report", "plugins", "update", "connect", "feeds",
               "log", "setup", "eval", "hud", "vision", "night", "timeline", "relay")

# Subcommands without own argparse: shows this text instead of running `alice <command> --help`
# (e.g. prevent `alice phone --help` from opening bridge and waiting, `alice notes --help` listing notes).
_SUB_DOCS: dict[str, tuple[str, str | None]] = {
    "core": ("alice.core.cli", None), "rules": ("alice.core.cli", None), "tasks": ("alice.core.cli", None),
    "feeds": ("alice.core.cli", None), "log": ("", None), "voice": ("alice.voice.cli", None),
    "notes": ("alice.extras.cli", None), "brief": ("alice.extras.cli", None), "report": ("alice.extras.cli", None),
    "watch": ("alice.app.phone", "watch_cmd"), "phone": ("alice.app.phone", "phone_cmd"),
    "dcs": ("alice.sims.dcs", "dcs_cmd"), "relay": ("alice.app.relay", "relay_cmd"), "vision": ("alice.sims.vision", None),
    "night": ("alice.night", None), "timeline": ("alice.timeline", None), "plugins": ("alice.plugins", "plugins_cmd"), "quick": ("", None),
}

_HELP_ARGS = ("-h", "--help", "help")

# Optional package -> extra in pyproject.toml. If missing, prints what to install instead of traceback.
OPTIONAL_EXTRAS: dict[str, str] = {
    "mss": "computer", "pyautogui": "computer", "pyperclip": "computer", "pygetwindow": "computer",
    "pywinauto": "computer", "playwright": "browser", "webview": "app", "pystray": "core",
    "sounddevice": "voice", "pynput": "voice", "webrtcvad": "voice", "edge_tts": "voice", "av": "voice",
    "websockets": "voice", "faster_whisper": "voice-local", "openwakeword": "voice-local", "TTS": "voice-xtts",
}


def install_hint(module: str) -> str:
    """Single-line installation suggestion for a missing module."""
    root = module.split(".", 1)[0]
    extra = OPTIONAL_EXTRAS.get(root)
    if extra:
        return f'pip install "alice-harness[{extra}]"  (repo klasöründe: pip install -e ".[{extra}]")'
    return f"pip install {root}"


def subcommand_help(name: str) -> str:
    """`alice <command> --help` text: module's own usage text, otherwise its line in the main help."""
    import importlib

    mod, attr = _SUB_DOCS.get(name, ("", None))
    doc = ""
    if mod:
        m = importlib.import_module(mod)
        doc = (getattr(m, attr).__doc__ if attr else m.__doc__) or ""
    if not doc.strip():
        doc = "\n".join(ln for ln in (__doc__ or "").splitlines() if re.match(rf"\s+alice {re.escape(name)}\b", ln))
    return doc.strip("\n")


def unknown_command_hint(argv: list[str]) -> str | None:
    """If a single-word argument looks like a typo for a subcommand, suggest what was meant.

    Prevents `alice doctr` from opening REPL with a "doctr" message and querying model. For real single words, use `alice -p`."""
    import difflib

    if len(argv) != 1 or not re.fullmatch(r"[a-z][a-z-]{2,}", argv[0]) or argv[0] in SUBCOMMANDS:
        return None
    word = argv[0]
    if word == "status":
        return "Bilinmeyen komut: status. Çekirdeğin durumu için alice core status, anahtarlar ve modeller için alice doctor."
    if word == "version":
        return f"alice {package_version()} (sürüm için: alice --version)"
    close = difflib.get_close_matches(word, SUBCOMMANDS, n=1, cutoff=0.8)
    if not close:
        return None
    return (f"Bilinmeyen komut: {word}. Bunu mu demek istedin: alice {close[0]}? "
            f'(ALICE\'e mesaj olarak göndermek için: alice -p "{word}")')


def _main_parser() -> TrArgumentParser:
    ap = TrArgumentParser(prog="alice", description="ALICE: ücretsiz API modelleriyle çalışan ajan harness.",
                          epilog="alt komutlar:\n" + "\n".join((__doc__ or "").splitlines()[2:]).rstrip())
    ap.add_argument("prompt", nargs="*", help="ilk mesaj (verilirse REPL bu mesajla başlar)")
    ap.add_argument("-p", "--print", dest="oneshot", action="store_true",
                    help="tek seferlik çalıştır ve çık (mesaj yoksa stdin'den okunur)")
    ap.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text",
                    help="-p ile: text (varsayılan), json (sonunda tek nesne) ya da stream-json (her olay bir satır)")
    ap.add_argument("--mode", choices=MODES, help="izin modu (varsayılan: config permission_mode, genelde auto)")
    ap.add_argument("--yolo", action="store_true", help="hiçbir izin sorma (tam erişim), --mode yolo ile aynı")
    ap.add_argument("--cwd", type=Path, help="çalışma klasörü (varsayılan: bulunduğun klasör)")
    ap.add_argument("--model", help="beyin rolünü tek modele sabitle, ör. gemini:gemini-flash-latest")
    ap.add_argument("--resume", nargs="?", const="", metavar="ID",
                    help="son (ya da verilen id'li) oturumu devam ettir")
    ap.add_argument("-v", "--verbose", action="store_true", help="hangi modelin cevap verdiğini göster")
    ap.add_argument("--version", action="version", version=f"alice {package_version()}",
                    help="sürümü göster ve çık")
    return ap


def _reflex_parser() -> TrArgumentParser:
    rp = TrArgumentParser(prog="alice reflex", description="Jev/System One reflex döngüsünü tek başına çalıştır.")
    rp.add_argument("goal", help='hedef, ör. "Ayarlar sayfasını açıp Gizlilik sekmesine geç"')
    rp.add_argument("--target", choices=["browser", "desktop"], default="browser",
                    help="browser: tarayıcı sayfası (varsayılan), desktop: Windows penceresi (UI Automation)")
    rp.add_argument("--url", help="browser: önce bu adresi aç")
    rp.add_argument("--title", help="desktop: pencere başlığı parçası")
    rp.add_argument("--input", action="append", help="alan=metin (tekrarlanabilir)")
    rp.add_argument("--max-steps", type=int, default=25, help="en fazla adım (varsayılan 25)")
    rp.add_argument("--close", action="store_true", help="bitince tarayıcıyı hemen kapat")
    return rp


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.stdout is None or sys.stderr is None:
        # No stdout under pythonw.exe (console-less shortcut); swallow output.
        import os

        devnull = open(os.devnull, "w", encoding="utf-8")
        try:  # keep errors from being lost: ~/.alice/core.log (rotate when > 2 MB)
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
            pass  # the console stays in its own encoding
    try:
        return _dispatch(argv)
    except ModuleNotFoundError as e:
        # Optional package missing (e.g. sounddevice for voice): tell what to install instead of traceback.
        name = e.name or ""
        if name.split(".", 1)[0] not in OPTIONAL_EXTRAS:
            raise
        console.print(f"[red]Eksik paket: {escape(name)}.[/] Kur: [bold]{escape(install_hint(name))}[/]")
        return 1
    except KeyboardInterrupt:
        console.print("[dim]durduruldu[/]")
        return 130


def _dispatch(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd in _SUB_DOCS and argv[1:2] and argv[1] in _HELP_ARGS:
        console.print(subcommand_help(cmd), markup=False, highlight=False)
        return 0
    if cmd == "help":
        _main_parser().print_help()
        return 0
    if cmd == "init":
        ip = TrArgumentParser(prog="alice init", description="~/.alice altında config.toml, .env ve personality.md "
                              "oluştur (var olanlara dokunmaz).")
        ip.add_argument("--force", action="store_true", help="var olan dosyaların üzerine varsayılanı yaz")
        a = ip.parse_args(argv[1:])
        for p in init_home(force=a.force):
            console.print(f"[green]oluşturuldu:[/] {p}")
        console.print(f"Şimdi {alice_home() / '.env'} dosyasına API anahtarlarını yaz, sonra [bold]alice doctor[/].")
        return 0
    if cmd == "log":
        from alice import actionlog

        rows = actionlog.recent(int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 30)
        for r in reversed(rows):
            console.print(escape(actionlog.format_row(r)))
        if not rows:
            console.print("[dim]kayıt yok: ALICE kimseye bir şey göndermedi[/]")
        return 0
    if cmd == "setup":
        from alice.setup_google import setup_cmd

        return setup_cmd(argv[1:])
    if cmd == "eval":
        from alice.evals import eval_cmd

        return eval_cmd(argv[1:])
    if cmd == "connect":
        from alice.connect.cli import connect_cmd

        return connect_cmd(argv[1:])
    if cmd == "update":
        return update_cmd(argv[1:])
    if cmd == "doctor":
        TrArgumentParser(prog="alice doctor", description="Her sağlayıcıya test isteği atar, karar motorunu dener, "
                         "eksik opsiyonel paketleri gösterir.").parse_args(argv[1:])
        return doctor(load_config())
    if cmd == "models":
        mp = TrArgumentParser(prog="alice models", description="Ücretsiz modelleri canlı ölç ve sırala "
                              "(sonuç ~/.alice/models.json).")
        mp.add_argument("--refresh", action="store_true", help="önbelleği boşver, şimdi yeniden ölç (~1-2 dk)")
        mp.add_argument("--write", action="store_true", help="config.toml'daki brain/fast/vision zincirlerini auto yap")
        a = mp.parse_args(argv[1:])
        return models_cmd(load_config(), a.refresh, a.write)
    if cmd == "app":
        try:
            return app_cmd(argv[1:])
        except Exception:
            # Error not visible when launched from shortcut (no console); write to file.
            from alice.app.server import log_error

            log_error()
            raise
    if cmd in ("core", "rules", "tasks", "feeds"):
        from alice.core import cli as core_cli

        return {"core": core_cli.core_cmd, "rules": core_cli.rules_cmd, "tasks": core_cli.tasks_cmd,
                "feeds": core_cli.feeds_cmd}[cmd](argv[1:])
    if cmd in ("notes", "brief", "report"):
        from alice.extras import cli as extras_cli

        return getattr(extras_cli, f"{cmd}_cmd")(argv[1:])
    if cmd in ("watch", "phone"):
        from alice.app import phone

        return (phone.watch_cmd if cmd == "watch" else phone.phone_cmd)(argv[1:])
    if cmd == "relay":
        from alice.app.relay import relay_cmd

        return relay_cmd(argv[1:])
    if cmd == "dcs":
        from alice.sims.dcs import dcs_cmd

        return dcs_cmd(argv[1:])
    if cmd == "quick":
        from alice.app.quick import quick_cmd

        return quick_cmd(argv[1:])
    if cmd == "hud":
        from alice.hud.window import hud_cmd

        return hud_cmd(argv[1:])
    if cmd == "vision":
        from alice.sims.vision import vision_cmd

        return vision_cmd(argv[1:])
    if cmd == "night":
        from alice.night import night_cmd

        return night_cmd(argv[1:])
    if cmd == "timeline":
        from alice.timeline import timeline_cmd

        return timeline_cmd(argv[1:])
    if cmd == "voice":
        from alice.voice.cli import voice_cmd

        return voice_cmd(argv[1:])
    if cmd == "shortcut":
        TrArgumentParser(prog="alice shortcut", description="ALICE kısayolu koy: Windows'ta masaüstü ve Başlat menüsü, "
                         "Linux'ta uygulama menüsü ve masaüstü.").parse_args(argv[1:])
        return shortcut_cmd()
    if cmd == "plugins":
        from alice.plugins import plugins_cmd

        return plugins_cmd(argv[1:])
    if cmd == "reflex":
        return reflex_cmd(load_config(), _reflex_parser().parse_args(argv[1:]))

    hint = unknown_command_hint(argv)
    if hint:
        console.print(hint, markup=False, highlight=False)
        return 2
    a = _main_parser().parse_args(argv)

    if a.model:
        try:
            parse_ref(a.model)
        except ValueError as e:
            console.print(f"[red]--model geçersiz:[/] {escape(str(e))}, ör. gemini:gemini-flash-latest")
            return 2

    json_out = None
    if a.oneshot and a.output_format != "text":
        json_out = sys.stdout  # only JSON to stdout; everything else to stderr
        console.file = sys.stderr
    cwd = (a.cwd or Path.cwd()).resolve()
    if not cwd.is_dir():
        console.print(f"[red]klasör yok:[/] {escape(str(cwd))}")
        return 2
    refresh_home()  # key lines of new providers to .env (does not touch existing ones)
    cfg = load_config(cwd=cwd)
    mode = "yolo" if a.yolo else (a.mode or cfg.agent.get("permission_mode", "auto"))
    prompt = " ".join(a.prompt).strip()
    if a.oneshot and not prompt:
        prompt = sys.stdin.read().strip() if sys.stdin is not None and not sys.stdin.isatty() else ""
        if not prompt:
            console.print('[red]-p için görev yok.[/] Kullanım: alice -p "görev" (ya da görevi stdin\'den ver)')
            return 2
    agent, router, ctx = build(cfg, cwd, mode, a.verbose)
    if json_out is not None:
        agent.ui = JsonUI(json_out, stream=a.output_format == "stream-json")
    if a.model:
        router.role_overrides["brain"] = [a.model]
    if not _check_ready(router):
        return 2
    if a.oneshot:
        t0 = time.monotonic()
        try:
            out = agent.run_turn(prompt)
        finally:
            cleanup(ctx)
        if json_out is not None:
            ui = agent.ui
            json_out.write(json.dumps({"type": "result", "result": out or "", "is_error": not out,
                                       "models": getattr(ui, "models", []), "tools": getattr(ui, "tools", []),
                                       "notes": getattr(ui, "errors", []),
                                       "duration_s": round(time.monotonic() - t0, 2)}, ensure_ascii=False) + "\n")
            json_out.flush()
        return 0 if out else 1
    if prompt:
        agent.run_turn(prompt)
    repl(agent, router, ctx, cfg, a.resume)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
