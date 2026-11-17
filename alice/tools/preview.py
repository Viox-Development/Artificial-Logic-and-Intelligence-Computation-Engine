"""preview: run the app, open it in the built-in browser and verify the change (Claude Code's "run / verify"
flow). It gives the model the screenshot and separately lists the page's console errors and failed network
requests: "page opened" and "page works correctly" are different things."""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from typing import Any

from alice.tools.base import EXEC, READ, Tool, ToolContext, ToolError, ToolResult, obj


def _reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 - the user's local server
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except (OSError, ValueError):
        return False


def format_logs(rows: list[dict[str, Any]], limit: int = 40) -> str:
    if not rows:
        return "Konsolda hata/uyarı yok, başarısız ağ isteği yok."
    kinds = {"console": "konsol", "exception": "yakalanmamış hata", "log": "tarayıcı", "network": "ağ"}
    out = [f"- [{kinds.get(r['kind'], r['kind'])} {r['level']}] {' '.join(str(r['text']).split())[:400]}"
           for r in rows[-limit:]]
    errors = sum(1 for r in rows if r["level"] == "error")
    return f"{errors} hata, {len(rows) - errors} uyarı:\n" + "\n".join(out)


class Preview(Tool):
    name = "preview"
    description = (
        "Web arayüzü değişikliğini gerçekten doğrular: (gerekirse) geliştirme sunucusunu başlatır, sayfayı ALICE'in "
        "yerleşik tarayıcısında açar (kullanıcı panelde canlı görür), ekran görüntüsünü ve sayfanın konsol "
        "hatalarını / başarısız ağ isteklerini döndürür. Arayüzde bir şey değiştirdikten sonra 'çalışıyor' demeden "
        "önce kullan.\n"
        "action=open: url (ör. http://localhost:5173); command verilirse önce onu arka planda başlatır (ör. npm run "
        "dev) ve sunucu cevap verene kadar bekler. action=check: şu anki sayfayı (ya da url'i) yeniden yükleyip "
        "görüntü ve hataları al. action=logs: yalnız son hatalar.\n"
        "Sayfada tıklama/yazma için browser aracını kullan; aynı tarayıcıdır.")
    parameters = obj({
        "action": {"type": "string", "enum": ["open", "check", "logs"], "description": "Yapılacak iş."},
        "url": {"type": "string", "description": "open: açılacak adres. check: boşsa şu anki sayfa."},
        "command": {"type": "string", "description": "open: önce başlatılacak sunucu komutu (arka planda)."},
        "wait": {"type": "integer", "description": "open: sunucunun cevap vermesi için en fazla saniye. Varsayılan 60."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        return EXEC if args.get("command") else READ

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('url') or ''}".strip()

    def run(self, ctx: ToolContext, action: str, url: str = "", command: str = "", wait: int = 60) -> ToolResult:
        factory = ctx.state.get("builtin_browser")
        if not callable(factory):
            raise ToolError("yerleşik tarayıcı yalnız ALICE uygulamasında var; tarayıcı aracını kullan")
        from alice.app.builtin_browser import BrowserError

        bb = factory()
        try:
            if action == "logs":
                return ToolResult(format_logs(bb.logs()))
            started = time.time()
            note = ""
            if action == "open":
                if not url:
                    raise ToolError("open için url gerekli")
                if command:
                    note = self._serve(ctx, command, url, max(5, min(int(wait or 60), 300)))
                bb.navigate(url)
            elif action == "check":
                if url:
                    bb.navigate(url)
                else:
                    bb.history("reload")
            else:
                raise ToolError(f"bilinmeyen action: {action}")
            loaded = bb.wait_load(20)
            time.sleep(1.2)  # first paint and late-arriving requests
            png = bb.screenshot_png()
            tab = bb.current()
            logs = bb.logs(since=started)
        except BrowserError as e:
            raise ToolError(f"yerleşik tarayıcı: {e}") from e
        head = f"Sayfa: {tab.get('title') or '(başlıksız)'} — {tab.get('url')}" + ("" if loaded else " (yüklenmesi bitmedi)")
        return ToolResult("\n".join(x for x in (note, head, format_logs(logs),
                                                "Ekran görüntüsü ekte; kullanıcı da panelde görüyor.") if x),
                          images=[png] if png else [])

    @staticmethod
    def _serve(ctx: ToolContext, command: str, url: str, wait: int) -> str:
        from alice.tools.shell import RunCommand

        if _reachable(url):
            return "Sunucu zaten çalışıyordu; komut çalıştırılmadı."
        res = RunCommand().run(ctx, command, background=True)
        m = re.search(r"pid=(\d+)", res.text)
        proc = ctx.state.get("bg_procs", {}).get(int(m.group(1))) if m else None
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if _reachable(url):
                return f"Sunucu başladı (pid={proc.pid if proc else '?'})."
            if proc is not None and proc.popen.poll() is not None:
                tail = proc.log.read_bytes().decode("utf-8", errors="replace")[-1500:] if proc.log.exists() else ""
                raise ToolError(f"sunucu komutu bitti (kod {proc.popen.returncode}), sayfa açılmadı:\n{tail}")
            if ctx.cancelled():
                raise ToolError("durduruldu")
            time.sleep(0.5)
        raise ToolError(f"{wait} sn içinde {url} cevap vermedi (sunucu pid={proc.pid if proc else '?'} arkada sürüyor)")


TOOLS: list[Tool] = [Preview()]
