"""Ekran, fare ve klavye kontrolü (pixel seviyesinde bilgisayar kullanımı).

Koordinatlar her zaman SON ekran görüntüsünün (küçültülmüş) piksel uzayındadır;
araç bunları gerçek ekran koordinatına kendisi çevirir. pyautogui FAILSAFE açık:
fareyi ekranın sol üst köşesine götürmek ALICE'in fare/klavye hareketini anında keser.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from alice.tools.base import COMPUTER, READ, Tool, ToolContext, ToolError, ToolResult, obj

_DPI_SET = False

_TRUE = {"true", "1", "yes", "on", "evet", "e"}
_FALSE = {"false", "0", "no", "off", "hayır", "hayir", "h", "", "none", "null"}


def as_bool(value: Any, name: str = "değer") -> bool:
    """Modelin gönderdiği bayrağı bool'a çevir: "false" metni True sayılmasın (bool("false") is True)."""
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
    raise ToolError(f"{name} true ya da false olmalı, '{value}' geldi")


def as_number(value: Any, name: str, kind: type = float) -> Any:
    """"120", 120.0 gibi değerleri sayıya çevir; çevrilemezse modelin düzeltebileceği bir ToolError."""
    if isinstance(value, bool):
        raise ToolError(f"{name} sayı olmalı, {value} geldi")
    try:
        num = float(str(value).strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} sayı olmalı, '{value}' geldi") from None
    if num != num or num in (float("inf"), float("-inf")):
        raise ToolError(f"{name} sonlu bir sayı olmalı, '{value}' geldi")
    return int(num) if kind is int else num


def _dpi_aware() -> None:
    """Windows'ta ölçekleme (%125/%150) varken mss ile pyautogui'nin aynı pikselleri görmesi için."""
    global _DPI_SET
    if _DPI_SET or sys.platform != "win32":
        return
    _DPI_SET = True
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _pyautogui():
    _dpi_aware()
    try:
        import pyautogui
    except ImportError as e:
        raise ToolError("pyautogui kurulu değil: pip install 'alice-harness[computer]'") from e
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    return pyautogui


def grab_screen(ctx: ToolContext, grid: bool = False, monitor: int = 1) -> tuple[str, str]:
    """Ekran görüntüsü al, ölçeği ctx.state'e kaydet. (b64 png, açıklama) döner."""
    _dpi_aware()
    try:
        import mss
    except ImportError as e:
        raise ToolError("mss kurulu değil: pip install 'alice-harness[computer]'") from e
    from PIL import Image, ImageDraw

    from alice.tools.imaging import image_to_png_b64

    with mss.mss() as sct:
        mons = sct.monitors
        if monitor >= len(mons):
            raise ToolError(f"monitör {monitor} yok, {len(mons) - 1} monitör var")
        mon = mons[monitor]
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    max_w = int(ctx.config.agent.get("screenshot_max_width", 1366))
    scale = img.width / max_w if img.width > max_w else 1.0
    if grid:
        # Izgara, küçültülmüş uzayda her 100 pikselde bir etiketlenecek şekilde orijinale çizilir.
        d = ImageDraw.Draw(img)
        step = 100 * scale
        x = step
        while x < img.width:
            d.line([(x, 0), (x, img.height)], fill=(255, 0, 255), width=max(1, int(scale)))
            d.text((x + 2, 2), str(int(round(x / scale))), fill=(255, 0, 255))
            x += step
        y = step
        while y < img.height:
            d.line([(0, y), (img.width, y)], fill=(255, 0, 255), width=max(1, int(scale)))
            d.text((2, y + 2), str(int(round(y / scale))), fill=(255, 0, 255))
            y += step
    b64, scale = image_to_png_b64(img, max_w)
    ctx.state["screen"] = {"scale": scale, "left": mon["left"], "top": mon["top"],
                           "w": round(img.width / scale), "h": round(img.height / scale)}
    s = ctx.state["screen"]
    return b64, f"ekran görüntüsü {s['w']}x{s['h']} (gerçek {img.width}x{img.height}, monitör {monitor})"


def to_real(ctx: ToolContext, x: float, y: float) -> tuple[int, int]:
    s = ctx.state.get("screen")
    if not s:
        raise ToolError("önce action=screenshot al; koordinatlar ekran görüntüsüne göre verilir")
    return int(s["left"] + x * s["scale"]), int(s["top"] + y * s["scale"])


def type_text(text: str) -> None:
    pg = _pyautogui()
    if text.isascii():
        pg.write(text, interval=0.01)
        return
    # Türkçe karakterler pyautogui.write ile yazılamıyor; panodan yapıştır.
    try:
        import pyperclip
    except ImportError as e:
        raise ToolError("ASCII olmayan metin için pyperclip gerekli") from e
    old = None
    try:
        old = pyperclip.paste()
    except Exception:  # noqa: BLE001
        pass
    pyperclip.copy(text)
    pg.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")
    # Hedef uygulama yapıştırmayı geç işleyebilir (meşgul sekme, WhatsApp): pano erken geri yüklenirse kullanıcının
    # eski panosu (şifre olabilir) yapıştırılıyordu.
    time.sleep(0.5)
    if old is not None:
        try:
            pyperclip.copy(old)
        except Exception:  # noqa: BLE001
            pass


def press_keys(keys: str) -> None:
    pg = _pyautogui()
    parts = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
    alias = {"control": "ctrl", "cmd": "command", "win": "win", "windows": "win", "return": "enter",
             "esc": "escape", "del": "delete", "pgup": "pageup", "pgdn": "pagedown"}
    parts = [alias.get(p, p) for p in parts]
    if len(parts) == 1:
        pg.press(parts[0])
    else:
        pg.hotkey(*parts)


def hold_keys(keys: str, seconds: float) -> None:
    """Tuşları (ör. 'shift+w') birlikte basılı tut, sonra ters sırayla bırak: oyunda koşmak, yürümek."""
    pg = _pyautogui()
    alias = {"control": "ctrl", "win": "win", "windows": "win", "return": "enter", "esc": "escape"}
    parts = [alias.get(k.strip().lower(), k.strip().lower()) for k in keys.replace(" ", "").split("+") if k.strip()]
    try:
        for k in parts:
            pg.keyDown(k)
        time.sleep(max(0.05, min(float(seconds), 10.0)))
    finally:
        for k in reversed(parts):
            pg.keyUp(k)


class Computer(Tool):
    name = "computer"
    description = (
        "Ekranın görüntüsünü alır ve fare/klavyeyi tek tek adımlarla kullanır (düşük seviye).\n"
        "- Önce action=screenshot al; x,y koordinatları o görüntünün piksel uzayındadır. Görmeden koordinat "
        "tahmin etme.\n"
        "- Ekran işleri (tek ya da çok adımlı) için önce reflex (hızlı); web sayfaları için browser; reflex "
        "yapamazsa computer_task. Koordinatla tıklama son çaredir.\n"
        "- 'Ekranda ne var' sorusunda screenshot yeter."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["screenshot", "click", "double_click", "right_click", "middle_click",
                                              "move", "drag", "scroll", "type", "key", "wait", "cursor_position"],
                   "description": "screenshot | click/double_click/right_click/middle_click/move: x,y | drag: x,y'den "
                   "to_x,to_y'ye | scroll: x,y üzerinde amount | type: text'i yaz | key: keys kombinasyonuna bas | "
                   "wait: seconds bekle | cursor_position."},
        "x": {"type": "number", "description": "Son ekran görüntüsünde yatay piksel."},
        "y": {"type": "number", "description": "Son ekran görüntüsünde dikey piksel."},
        "to_x": {"type": "number", "description": "drag için bitiş x."},
        "to_y": {"type": "number", "description": "drag için bitiş y."},
        "text": {"type": "string", "description": "type için yazılacak metin (Türkçe karakter olur)."},
        "keys": {"type": "string", "description": "key için tuş ya da kombinasyon, ör. 'enter', 'ctrl+s', 'alt+tab'."},
        "amount": {"type": "integer", "description": "scroll için çentik; pozitif yukarı, negatif aşağı. Varsayılan -5."},
        "seconds": {"type": "number", "description": "wait için saniye, en fazla 30. Varsayılan 1."},
        "grid": {"type": "boolean", "description": "screenshot'a koordinat ızgarası çiz (hassas tıklama için)."},
        "monitor": {"type": "integer", "description": "1 ana monitör, 2 ikinci, 0 hepsi yan yana. Varsayılan 1."},
        "screenshot_after": {"type": "boolean", "description": "İşlemden sonra yeni ekran görüntüsü döndür."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") in ("screenshot", "cursor_position", "wait") else COMPUTER

    def run(self, ctx: ToolContext, action: str, x: float | None = None, y: float | None = None,
            to_x: float | None = None, to_y: float | None = None, text: str | None = None,
            keys: str | None = None, amount: int = -5, seconds: float = 1.0, grid: bool = False,
            monitor: int = 1, screenshot_after: bool = False) -> ToolResult:
        monitor = as_number(1 if monitor is None else monitor, "monitor", int)
        if monitor < 0:
            raise ToolError(f"monitor 0 ya da pozitif olmalı, {monitor} geldi")
        screenshot_after = as_bool(screenshot_after, "screenshot_after")
        x, y, to_x, to_y = (None if v is None else as_number(v, n) for v, n in
                            ((x, "x"), (y, "y"), (to_x, "to_x"), (to_y, "to_y")))
        if action == "screenshot":
            b64, desc = grab_screen(ctx, grid=as_bool(grid, "grid"), monitor=monitor)
            return ToolResult(desc, images=[b64])
        if action == "wait":
            seconds = as_number(1.0 if seconds is None else seconds, "seconds")
            time.sleep(max(0.0, min(seconds, 30.0)))
            msg = f"{seconds} sn beklendi"
        elif action == "cursor_position":
            pg = _pyautogui()
            px, py = pg.position()
            s = ctx.state.get("screen") or {"scale": 1.0, "left": 0, "top": 0}
            return ToolResult(f"imleç: ({(px - s['left']) / s['scale']:.0f}, {(py - s['top']) / s['scale']:.0f}) ekran görüntüsü uzayında")
        elif action == "type":
            if text is None:
                raise ToolError("type için text gerekli")
            text = str(text)
            type_text(text)
            msg = f"{len(text)} karakter yazıldı"
        elif action == "key":
            if not keys:
                raise ToolError("key için keys gerekli")
            keys = str(keys)
            press_keys(keys)
            msg = f"tuş: {keys}"
        else:
            pg = _pyautogui()
            if x is None or y is None:
                raise ToolError(f"{action} için x ve y gerekli")
            rx, ry = to_real(ctx, x, y)
            if action in ("click", "double_click", "right_click", "middle_click"):
                button = {"right_click": "right", "middle_click": "middle"}.get(action, "left")
                pg.click(rx, ry, clicks=2 if action == "double_click" else 1, button=button, interval=0.08)
            elif action == "move":
                pg.moveTo(rx, ry, duration=0.1)
            elif action == "drag":
                if to_x is None or to_y is None:
                    raise ToolError("drag için to_x ve to_y gerekli")
                tx, ty = to_real(ctx, to_x, to_y)
                pg.moveTo(rx, ry)
                pg.dragTo(tx, ty, duration=0.4, button="left")
            elif action == "scroll":
                pg.moveTo(rx, ry)
                # Windows'ta birim wheel delta (120 = bir çentik), X11'de çentik, macOS'ta satır.
                unit = {"win32": 120, "darwin": 5}.get(sys.platform, 1)
                pg.scroll(as_number(-5 if amount is None else amount, "amount", int) * unit)
            msg = f"{action} ({x:.0f},{y:.0f})"
        if screenshot_after:
            time.sleep(0.4)
            b64, desc = grab_screen(ctx, monitor=monitor)
            return ToolResult(f"{msg}\n{desc}", images=[b64])
        return ToolResult(msg)


class ComputerTask(Tool):
    name = "computer_task"
    description = (
        "YAVAŞ yedek: ekran görüntüsüne bakan büyük modelle çok adımlı ekran işi yapar (adım başına 5-15 sn, "
        "dakikalar sürer). Ekran işlerinde önce reflex'i kullan: o da çok adımlıdır (yazma, kısayol, kaydet "
        "penceresi, menüler) ve adım başına ~1 sn. Not Defteri'ne yazıp kaydetmek bununla 9 dakika sürmüştü.\n"
        "- Kullan: reflex needs_help dediğinde, ya da ekranda okunabilir öğe/yazı olmayan arayüzlerde.\n"
        "- Kullanma: reflex'in yapabildiği işler, uygulama açmak (windows open), dosya ya da komut işleri (dosya "
        "araçları, run_command). Aynı anda tek görev çalışır; sürerken tekrar başlatma."
    )
    parameters = obj({
        "goal": {"type": "string", "description": "Yapılacak iş; kullanıcının istediği gibi, adlarıyla, net ve "
                 "eksiksiz. Ör. 'Blender'ın arayüzünde Render ayarlarını aç'."},
        "monitor": {"type": "integer", "description": "Çalışılacak monitör (1, 2...). Boş: öndeki pencerenin monitörü."},
        "max_steps": {"type": "integer", "description": "En fazla adım. Varsayılan 30."},
    }, ["goal"])
    risk = COMPUTER

    def run(self, ctx: ToolContext, goal: str, max_steps: int = 30, monitor: int = 0) -> ToolResult:
        from alice.computer_agent import ComputerAgent, Screen, router_asker

        if ctx.router is None:
            raise ToolError("model yönlendirici yok")
        from alice.computer_agent import _RUNNING, log_line

        if not _RUNNING.acquire(blocking=False):
            return ToolResult("zaten bir ekran görevi sürüyor; bitmesini bekle, aynı işi tekrar başlatma.", is_error=True)
        try:
            log_line(f"başladı: {goal}")

            def emit(s: str) -> None:
                ctx.emit(s)
                log_line(s)

            agent = ComputerAgent(Screen(ctx, monitor=max(0, as_number(monitor or 0, "monitor", int))),
                                  router_asker(ctx.router, str(ctx.config.agent.get("computer_role", "vision"))),
                                  emit=emit, stop=ctx.cancelled)
            res = agent.run(goal, max_steps=max(1, min(as_number(max_steps or 30, "max_steps", int), 60)))
            log_line(f"bitti: {res.status}: {res.summary}")
        finally:
            _RUNNING.release()
        return ToolResult(res.report(), is_error=res.status in ("error",))


TOOLS = [Computer(), ComputerTask()]
