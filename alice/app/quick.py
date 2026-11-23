"""Quick bar: a small ALICE box opened with a hotkey regardless of what program you are in.

  Ctrl+Alt+A  quick bar (type or hold the mic button to talk)
  Ctrl+Alt+S  select screen region and ask (error message, Unity console...)
  Ctrl+Alt+C  explain clipboard text (a copied error)

Runs in the background with its own local server and agent; the window opens in Edge/Chrome
address-bar-less app mode, so opening it is instantaneous. Hotkeys can be changed in config
under [quick]. If core (alice core run) is running, it starts alongside it.
"""

from __future__ import annotations

import base64
import io
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from alice.config import Config, alice_home

IDLE_NEW = 600  # if unused for this many seconds, quick bar opens with a new conversation

EXPLAIN_CLIPBOARD = "Panoya kopyaladığım şunu kısaca açıkla; bir hataysa nedenini ve çözümünü söyle:\n\n{text}"


def screen_size() -> tuple[int, int]:
    if sys.platform == "win32":
        try:
            import ctypes

            u = ctypes.windll.user32  # type: ignore[attr-defined]
            return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        except (OSError, AttributeError):
            pass  # user32 unavailable: use the fallback size
    return 1920, 1080


def png_b64(img, max_width: int = 1600) -> str:
    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def select_region():
    """Dim the screen, let the user select a region with mouse. Returns PIL image of selected region (Esc = None)."""
    import tkinter as tk

    from alice.tools.imaging import screen_capture
    from PIL import Image, ImageEnhance, ImageTk

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            pass  # not Windows or already set: keep the default
    with screen_capture() as s:
        mon = s.monitors[0]  # all monitors
        shot = s.grab(mon)
        full = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    result: dict[str, Any] = {}
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.geometry(f"{mon['width']}x{mon['height']}+{mon['left']}+{mon['top']}")
    dim = ImageTk.PhotoImage(ImageEnhance.Brightness(full).enhance(0.55))
    canvas = tk.Canvas(root, width=mon["width"], height=mon["height"], highlightthickness=0, cursor="crosshair")
    canvas.pack()
    canvas.create_image(0, 0, image=dim, anchor="nw")
    canvas.create_text(mon["width"] // 2, 30, text="Sormak istediğin alanı seç · Esc vazgeç", fill="white",
                       font=("Segoe UI", 14))
    rect = {"id": None, "x": 0, "y": 0, "bright": None, "photo": None}

    def down(e):
        rect["x"], rect["y"] = e.x, e.y

    def move(e):
        x0, y0, x1, y1 = min(rect["x"], e.x), min(rect["y"], e.y), max(rect["x"], e.x), max(rect["y"], e.y)
        for k in ("id", "bright"):
            if rect[k] is not None:
                canvas.delete(rect[k])
        if x1 - x0 > 2 and y1 - y0 > 2:
            rect["photo"] = ImageTk.PhotoImage(full.crop((x0, y0, x1, y1)))
            rect["bright"] = canvas.create_image(x0, y0, image=rect["photo"], anchor="nw")
        rect["id"] = canvas.create_rectangle(x0, y0, x1, y1, outline="#d97757", width=2)

    def up(e):
        x0, y0, x1, y1 = min(rect["x"], e.x), min(rect["y"], e.y), max(rect["x"], e.x), max(rect["y"], e.y)
        if x1 - x0 > 8 and y1 - y0 > 8:
            result["img"] = full.crop((x0, y0, x1, y1))
        root.destroy()

    canvas.bind("<ButtonPress-1>", down)
    canvas.bind("<B1-Motion>", move)
    canvas.bind("<ButtonRelease-1>", up)
    root.bind("<Escape>", lambda e: root.destroy())
    root.focus_force()
    root.mainloop()
    return result.get("img")


def read_clipboard() -> str:
    try:
        import pyperclip

        return pyperclip.paste() or ""
    except Exception:
        pass  # no clipboard access: start empty
    try:
        import tkinter as tk

        r = tk.Tk()
        r.withdraw()
        try:
            return r.clipboard_get()
        finally:
            r.destroy()
    except Exception:
        return ""


class QuickBar:
    def __init__(self, cfg: Config, cwd: Path | None = None, state=None):
        from alice.app.server import AppServer, AppState

        self.cfg = cfg
        q = cfg.data.get("quick", {})
        self.hotkeys = {"open": q.get("hotkey", "<ctrl>+<alt>+a"), "region": q.get("region_hotkey", "<ctrl>+<alt>+s"),
                        "clipboard": q.get("clipboard_hotkey", "<ctrl>+<alt>+c")}
        self.state = state or AppState(cfg, cwd or Path.home(), cfg.agent.get("permission_mode", "auto"))
        self.server = AppServer(self.state, owns_state=state is None).start()
        self.last_open = 0.0
        self._listener = None
        self.hotkey_problem = ""
        self.launcher = self._launch
        self._write_url()

    def _write_url(self) -> None:
        """Let `alice quick --open` (a desktop shortcut on Wayland) find this running quick bar."""
        try:
            path = url_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.url, encoding="utf-8")
            path.chmod(0o600)  # the URL carries the token
        except OSError:
            pass  # only the Wayland shortcut path needs it

    @property
    def url(self) -> str:
        port = self.server.httpd.server_address[1]
        return f"http://127.0.0.1:{port}/quick?t={self.state.token}"

    def open(self, text: str = "", image: str | None = None, autosend: bool = False, listen: bool = False) -> None:
        now = time.monotonic()
        if self.last_open and now - self.last_open > IDLE_NEW and not (self.state.agent.busy or self.state._queued):
            self.state.new_session()
        self.last_open = now
        pending = {k: v for k, v in {"text": text, "image": image, "autosend": autosend, "listen": listen}.items() if v}
        self.state.quick_pending = pending or None
        self.launcher(self.url)

    def ask_region(self) -> None:
        try:
            img = select_region()
        except Exception as e:  # if tkinter/mss is not available
            self.state.bus.publish({"type": "info", "text": f"alan seçilemedi: {e}"})
            return
        if img is not None:
            self.open(image=png_b64(img))

    def explain_clipboard(self) -> None:
        text = read_clipboard().strip()
        if text:
            self.open(text=EXPLAIN_CLIPBOARD.format(text=text[:6000]), autosend=True)
        else:
            self.open()

    @staticmethod
    def _launch(url: str) -> None:
        from alice.app.window import _browser_candidates

        w, h = 680, 440
        sw, sh = screen_size()
        x, y = (sw - w) // 2, max(40, sh // 5)
        for exe in _browser_candidates():
            profile = alice_home() / "quick-window"
            profile.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([exe, f"--app={url}", f"--window-size={w},{h}", f"--window-position={x},{y}",
                              f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        webbrowser.open(url)

    def start_hotkeys(self) -> bool:
        """Listen for global hotkeys. RegisterHotKey on Windows (no keyboard-slowing hook), pynput on others.
        False on failure."""
        def bg(fn):
            return lambda: threading.Thread(target=fn, daemon=True).start()

        mapping = {self.hotkeys["open"]: bg(self.open), self.hotkeys["region"]: bg(self.ask_region),
                   self.hotkeys["clipboard"]: bg(self.explain_clipboard)}
        from alice.hotkeys import IS_WIN, PynputHotkeys, WinHotkeys

        hk = WinHotkeys(mapping) if IS_WIN else PynputHotkeys(mapping)
        if hk.start():
            self._listener = hk
            return True
        self.hotkey_problem = getattr(hk, "problem", "") or "kısayollar başka bir program tarafından kullanılıyor"
        return False

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
        self.server.stop()
        try:
            if url_file().read_text(encoding="utf-8") == self.url:
                url_file().unlink()
        except OSError:
            pass  # already gone or not ours: the next start overwrites it


def url_file() -> Path:
    return alice_home() / "quick-url"


def open_running() -> bool:
    """Open the quick bar of an already running core/quick process (True), e.g. from a desktop shortcut."""
    import urllib.request

    try:
        url = url_file().read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not url.startswith("http://127.0.0.1:"):
        return False
    try:
        with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 - our own local server
            if r.status != 200:
                return False
    except OSError:
        return False
    QuickBar._launch(url)
    return True


def quick_cmd(argv: list[str]) -> int:
    from rich.console import Console
    from rich.markup import escape

    from alice.config import load_config

    console = Console(highlight=False)
    if "--open" in argv and open_running():  # a desktop shortcut while the core/quick bar is already running
        return 0
    cfg = load_config()
    qb = QuickBar(cfg)
    if "--open" in argv:
        qb.open()
    ok = qb.start_hotkeys()
    if not ok:
        why = getattr(qb, "hotkey_problem", "") or "kısayollar başlatılamadı"
        console.print(f"[yellow]Kısayollar çalışmıyor:[/] {escape(why)}. Hızlı bar adresi: {qb.url}")
    else:
        hk = qb.hotkeys
        console.print(f"[bold magenta]ALICE hızlı bar[/] hazır: {hk['open']} aç · {hk['region']} alan seç · "
                      f"{hk['clipboard']} panoyu açıklat. [dim]Ctrl+C çık[/]")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass  # Ctrl+C: shut down quietly
    finally:
        qb.stop()
    return 0
