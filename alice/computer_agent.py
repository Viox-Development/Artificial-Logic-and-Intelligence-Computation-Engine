"""Bilgisayar kullanma ajanı: Codex / Claude computer use gibi ekranı görerek fare ve klavyeyle iş yapar.

Her adım:
  1. bak    -> ekran görüntüsü + öndeki pencerenin UI Automation öğeleri; öğeler görüntüde numaralı kutularla
               işaretlenir (set-of-marks), model piksel tahmini yerine numarayla tıklayabilir
  2. karar  -> görme modeli (vision rolü) hedefe, geçmiş adımlara ve görüntüye bakıp TEK aksiyon seçer (JSON)
  3. yap    -> fare/klavye (kendi pencerelerinde de çalışır: Blender, oyun, UIA vermeyen uygulamalar)
  4. tekrar -> sonuç bir sonraki ekran görüntüsünde görülür, model kendini düzeltir

Sesli konuşmada (Live) bu döngü arka planda çalışır; bitince sonuç konuşmaya yazılır ve Alice söyler.
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

SYSTEM = """You operate a Windows computer for the user with the mouse and keyboard, like a careful human.
Each step you get: the GOAL, what you already did, and a fresh SCREENSHOT of the screen. Interactive elements of
the foreground window are marked with numbered boxes on the screenshot and listed as ELEMENTS.
Reply with the next action as a JSON object, nothing else:
{"thought": "<short: what you see and why this action>", "action": "<action>", ...arguments}
When several actions are certain from the CURRENT screenshot (e.g. click the message box, type the text, press
Enter), send them together to save time, up to 5, executed in order:
{"thought": "...", "actions": [{"action": "click", "id": 7}, {"action": "type", "text": "merhaba", "enter": true}]}
Do not batch past something you cannot see yet (a menu that will open, a page that will load).
Actions:
- click / double_click / right_click: {"id": <element number>} or {"point": [y, x]} (y,x normalized 0-1000)
- type: {"text": "..."} types into the focused field (click the field first); add "enter": true to press Enter after
- key: {"keys": "ctrl+s"} | "enter" | "win" | "alt+tab" | "escape" ...
- scroll: {"point": [y, x], "direction": "down"|"up", "amount": 1-10}
- drag: {"point": [y, x], "to": [y, x]}
- open: {"target": "<app name, file path or URL>"} launches an app/file/site (faster than clicking through menus)
- wait: {"seconds": 1-5} when something is loading
- done: {"summary": "<what was achieved, in Turkish, one sentence>"} when the GOAL is visibly achieved
- fail: {"summary": "<why it cannot be done, in Turkish>"} if impossible or it needs the user (login, payment...)
Rules: prefer element ids over points. Prefer keyboard shortcuts when reliable. Check the screenshot to verify
the previous action worked before continuing; if it did not, try a different way. Never enter passwords or pay.
Do not close or delete the user's things unless the GOAL says so."""


@dataclass
class Shot:
    png_b64: str  # modele giden (küçültülmüş, işaretli) görüntü
    left: int
    top: int
    width: int  # gerçek piksel
    height: int
    elements: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""


@dataclass
class TaskResult:
    status: str  # done | fail | max_steps | error | stopped
    summary: str
    steps: list[str]

    def report(self) -> str:
        tail = "\n".join(self.steps[-12:])
        return f"{self.status}: {self.summary}" + (f"\nadımlar:\n{tail}" if tail else "")


def parse_action(text: str) -> dict[str, Any]:
    text = text or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"JSON yok: {text[:120]}")
    a = json.loads(m.group(0))
    if isinstance(a.get("action"), dict):  # {"action": {"action": "click", ...}} ya da {"action": {"point": ...}}
        inner = a.pop("action")
        a = {**a, **inner}
        a.setdefault("action", "click" if ("point" in inner or "id" in inner) else "")
    return a


ACTIONS = {"click", "double_click", "right_click", "type", "key", "scroll", "drag", "open", "wait", "done", "fail"}


def normalize_action(b: dict[str, Any]) -> dict[str, Any]:
    """Model bazen aksiyon adını unutuyor ya da alanları karıştırıyor: {"key": "ctrl+f"},
    {"type": "stalk", "enter": true}. Alanlardan ne demek istediğini çıkar."""
    b = dict(b)
    kind = b.get("action")
    if isinstance(b.get("type"), str) and b["type"] not in ACTIONS and kind in (None, "", "type"):
        b.setdefault("text", b.pop("type"))  # {"type": "metin"}: yazılacak metin
        kind = b["action"] = "type"
    if isinstance(b.get("key"), str) and not b.get("keys"):
        b["keys"] = b.pop("key")
    if not kind:
        if b.get("type") in ACTIONS:
            kind = b.pop("type")
        elif b.get("keys"):
            kind = "key"
        elif "text" in b:
            kind = "type"
        elif b.get("target"):
            kind = "open"
        elif "seconds" in b:
            kind = "wait"
        elif "to" in b and "point" in b:
            kind = "drag"
        elif "direction" in b:
            kind = "scroll"
        elif "point" in b or "id" in b:
            kind = "click"
        elif "summary" in b:
            kind = "done"
        b["action"] = kind or ""
    return b


_RUNNING = threading.Lock()  # fare/klavye tek: aynı anda tek ekran görevi


def log_line(text: str) -> None:
    """Ekran görevinin adımlarını voice.log'a yaz (neden olmadı, sonradan anlaşılsın)."""
    try:
        from alice.config import alice_home

        with open(alice_home() / "voice.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} computer_task {text[:400]}\n")
    except OSError:
        pass


class Screen:
    """Gerçek ekran: görüntü + UIA öğeleri + fare/klavye."""

    def __init__(self, ctx, max_width: int = 1280, max_elements: int = 60, monitor: int = 0):
        self.ctx = ctx
        self.monitor = monitor  # 0: öndeki pencerenin monitörü
        self.max_width = max_width
        self.max_elements = max_elements

    def observe(self) -> Shot:
        import mss
        from PIL import Image, ImageDraw

        from alice.tools.computer import _dpi_aware

        _dpi_aware()
        elements: list[dict[str, Any]] = []
        title = ""
        try:
            from alice.tools.desktop import collect_ui_elements

            title, items = collect_ui_elements(self.ctx, limit=self.max_elements)
            elements = [i for i in items if i.get("rect")]
        except Exception:  # noqa: BLE001 - UIA yoksa sadece görüntü
            pass
        with mss.mss() as sct:
            mon = sct.monitors[self.monitor] if 0 < self.monitor < len(sct.monitors) else self._monitor(sct)
            raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        scale = min(1.0, self.max_width / img.width)
        if scale < 1:
            img = img.resize((round(img.width * scale), round(img.height * scale)))
        d = ImageDraw.Draw(img)
        for e in elements:
            l, t, r, b = e["rect"]
            box = [(l - mon["left"]) * scale, (t - mon["top"]) * scale, (r - mon["left"]) * scale,
                   (b - mon["top"]) * scale]
            if box[2] < 0 or box[3] < 0 or box[0] > img.width or box[1] > img.height:
                continue
            d.rectangle(box, outline=(255, 0, 200), width=2)
            d.rectangle([box[0], box[1], box[0] + 7 + 7 * len(str(e["id"])), box[1] + 13], fill=(255, 0, 200))
            d.text((box[0] + 2, box[1]), str(e["id"]), fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return Shot(base64.b64encode(buf.getvalue()).decode(), mon["left"], mon["top"], mon["width"],
                    mon["height"], elements, title)

    @staticmethod
    def _monitor(sct) -> dict[str, int]:
        mon = sct.monitors[1]
        try:
            import ctypes
            from ctypes import wintypes

            r = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(r))
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            for m in sct.monitors[1:]:
                if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                    return m
        except Exception:  # noqa: BLE001
            pass
        return mon

    def act(self, a: dict[str, Any], shot: Shot) -> str:
        from alice.tools.computer import _pyautogui, press_keys, type_text

        kind = a.get("action")
        if kind == "open":
            from alice.tools.desktop import open_app

            from alice.tools.desktop import is_script_target

            target = str(a.get("target", ""))
            if is_script_target(target):  # ekrandan okunan bir betiği onaysız çalıştırma
                raise ValueError("betik/kurulum dosyası ekran görevinden açılmaz; kullanıcıya sor")
            res = open_app(target)
            time.sleep(1.5)
            # Zaten açık uygulamayı yeniden "açmak" Windows'ta onu öne getirmiyor (öndeki oyun kilidi):
            # pencere varsa gerçekten öne al, yoksa model öndekinin hâlâ eski pencere olduğunu bilsin.
            name = target.strip()
            if name and "://" not in name and "\\" not in name and "/" not in name:
                try:
                    from alice.tools.desktop import focus_window

                    focus_window(name)
                except Exception as e:  # noqa: BLE001
                    if "öne alınamadı" in str(e):
                        res += f"; {e}"
            return res
        if kind == "wait":
            time.sleep(max(0.5, min(float(a.get("seconds", 1)), 5.0)))
            return "beklendi"
        if kind == "type":
            type_text(str(a.get("text", "")))
            if a.get("enter"):
                press_keys("enter")
            return f"yazıldı: {str(a.get('text', ''))[:60]}"
        if kind == "key":
            press_keys(str(a.get("keys", "")))
            return f"tuş: {a.get('keys')}"
        pg = _pyautogui()
        x, y = self.point(a, shot)
        if kind in ("click", "double_click", "right_click"):
            pg.click(x, y, clicks=2 if kind == "double_click" else 1, button="right" if kind == "right_click" else "left",
                     interval=0.08)
            return f"{kind} ({x},{y})"
        if kind == "scroll":
            pg.moveTo(x, y)
            n = max(1, min(int(a.get("amount", 3)), 10))
            pg.scroll((n if a.get("direction") == "up" else -n) * 120)
            return f"kaydırıldı {a.get('direction', 'down')} {n}"
        if kind == "drag":
            tx, ty = self.point({"point": a.get("to")}, shot)
            pg.moveTo(x, y)
            pg.dragTo(tx, ty, duration=0.5, button="left")
            return f"sürüklendi ({x},{y}) -> ({tx},{ty})"
        raise ValueError(f"bilinmeyen aksiyon: {kind}")

    @staticmethod
    def point(a: dict[str, Any], shot: Shot) -> tuple[int, int]:
        if a.get("id") is not None:
            for e in shot.elements:
                if e["id"] == int(a["id"]):
                    return tuple(e["center"])  # type: ignore[return-value]
            raise ValueError(f"öğe {a['id']} yok")
        p = a.get("point")
        if not p or len(p) != 2:
            raise ValueError("id ya da point gerekli")
        y, x = float(p[0]), float(p[1])
        return round(shot.left + x / 1000 * shot.width), round(shot.top + y / 1000 * shot.height)


# ------------------------------------------------------------------------------- deneyim
# Biten işlerin adımları (~/.alice/computer_recipes.json): benzer bir hedef gelince modele ipucu olarak verilir.
# Başarılı yol "bu sırayla oldu", başarısız deneme "bu yoldan gitme" der; aynı işte her seferinde sıfırdan aramaz.
RECIPES_MAX = 80


def recipes_path():
    from alice.config import alice_home

    return alice_home() / "computer_recipes.json"


def load_recipes() -> list[dict[str, Any]]:
    try:
        data = json.loads(recipes_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_recipe(goal: str, status: str, summary: str, steps: list[str], title: str = "") -> None:
    from alice.memory import terms

    mine = set(terms(goal))
    rows = [r for r in load_recipes() if not (set(terms(r.get("goal", ""))) == mine and r.get("status") == status)]
    rows.append({"goal": goal[:300], "status": status, "summary": summary[:300], "title": title[:120],
                 "steps": [re.sub(r"\s*\([^()]*\)$", "", s)[:220] for s in steps[-12:]], "ts": time.time()})
    path = recipes_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows[-RECIPES_MAX:], ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def similar_recipes(goal: str, min_score: float = 0.5) -> list[dict[str, Any]]:
    """Hedefe en çok benzeyen geçmiş başarı ve başarısızlık (en fazla birer tane)."""
    from alice.memory import terms

    mine = set(terms(goal))
    if not mine:
        return []
    best: dict[str, tuple[float, dict[str, Any]]] = {}
    for r in load_recipes():
        other = set(terms(r.get("goal", "")))
        if not other:
            continue
        score = len(mine & other) / len(mine | other)
        st = "done" if r.get("status") == "done" else "fail"
        if score >= min_score and score >= best.get(st, (0.0, {}))[0]:
            best[st] = (score, r)
    return [r for _, r in best.values()]


def recipe_hint(goal: str) -> str:
    out = []
    for r in similar_recipes(goal):
        if r.get("status") == "done":
            out.append(f"PAST SUCCESS on a similar goal ('{r['goal']}'). These steps worked; follow them if the screen "
                       "matches, verify each one:\n" + "\n".join(r.get("steps", [])))
        else:
            out.append(f"PAST FAILED ATTEMPT on a similar goal ('{r['goal']}'): {r.get('summary', '')}. Its last steps "
                       "(try a different way):\n" + "\n".join(r.get("steps", [])[-5:]))
    return "\n".join(out)


class ComputerAgent:
    def __init__(self, screen, ask: Callable[[list[dict[str, Any]]], str], emit: Callable[[str], None] = lambda s: None,
                 settle: float = 0.5, stop: Callable[[], bool] = lambda: False, learn: bool = True):
        self.screen = screen
        self.ask = ask  # mesajlar -> model cevabı (metin)
        self.emit = emit
        self.settle = settle
        self.stop = stop
        self.learn = learn  # biten işin adımlarını sakla, benzer işte ipucu ver

    def run(self, goal: str, max_steps: int = 30, max_seconds: float = 180.0) -> TaskResult:
        hint = recipe_hint(goal) if self.learn else ""
        if hint:
            self.emit("benzer bir işin geçmiş adımları ipucu olarak verildi")
        res = self._run(goal, max_steps, max_seconds, hint)
        if self.learn and res.status in ("done", "fail") and res.steps:
            save_recipe(goal, res.status, res.summary, res.steps)
        return res

    def _run(self, goal: str, max_steps: int, max_seconds: float, hint: str = "") -> TaskResult:
        steps: list[str] = []
        last: list[str] = []
        model_errors = 0
        deadline = time.monotonic() + max_seconds
        for n in range(1, max_steps + 1):
            if self.stop():
                return TaskResult("stopped", "durduruldu", steps)
            if time.monotonic() > deadline:
                return TaskResult("fail", f"{max_seconds:.0f} sn içinde bitmedi", steps)
            try:
                shot = self.screen.observe()
            except Exception as e:  # noqa: BLE001
                return TaskResult("error", f"ekran okunamadı: {e}", steps)
            elems = "\n".join(f"[{e['id']}] {e.get('role', '')} '{e.get('name', '')}'"
                              + (f" değer='{e['value']}'" if e.get("value") else "") for e in shot.elements[:60])
            text = (f"GOAL: {goal}\n" + (f"{hint}\n" if hint else "") + f"FOREGROUND WINDOW: {shot.title or '?'}\n"
                    f"DONE SO FAR:\n{chr(10).join(steps[-10:]) or '(nothing yet)'}\n"
                    f"ELEMENTS:\n{elems or '(none: use points)'}\nNext action JSON:")
            messages = [{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": [{"type": "text", "text": text}, {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{shot.png_b64}"}}]}]
            try:
                answer = self.ask(messages)
            except Exception as e:  # noqa: BLE001 - kota/bağlantı: görmeden devam etme
                model_errors += 1
                self.emit(f"görme modeli hatası: {str(e)[:200]}")
                if model_errors >= 2:
                    low = str(e).lower()
                    quota = "429" in low or "quota" in low or "soğumada" in low or "rate" in low
                    return TaskResult("error", "görme modeli kullanılamıyor" + (
                        " (günlük ücretsiz kota dolmuş)" if quota else f": {str(e)[:120]}"), steps)
                continue
            model_errors = 0
            try:
                a = parse_action(answer)
            except Exception as e:  # noqa: BLE001
                steps.append(f"{n}. model cevabı okunamadı: {e}")
                continue
            batch = a.get("actions") if isinstance(a.get("actions"), list) else [a]
            batch = [normalize_action(b) for b in batch if isinstance(b, dict)][:5]
            if not batch:
                steps.append(f"{n}. boş aksiyon")
                continue
            kind = batch[0].get("action", "")
            if kind in ("done", "fail"):
                return TaskResult(kind, str(batch[0].get("summary") or a.get("summary", "")), steps)
            key = json.dumps([{k: v for k, v in b.items() if k != "thought"} for b in batch], sort_keys=True,
                             ensure_ascii=False)
            last.append(key)
            if len(last) >= 3 and len(set(last[-3:])) == 1:
                return TaskResult("fail", "aynı adım tekrarlanıyor, ilerleyemedim", steps)
            results = []
            for b in batch:
                if b.get("action") in ("done", "fail"):
                    break
                try:
                    results.append(self.screen.act(b, shot))
                except Exception as e:  # noqa: BLE001
                    results.append(f"HATA {e}")
                    break  # sonrakiler bu adıma dayanıyor olabilir
                time.sleep(0.25)
            line = f"{n}. {key[:160]} -> {'; '.join(results)} ({str(a.get('thought', ''))[:100]})"
            steps.append(line)
            self.emit(line)
            time.sleep(self.settle)
        return TaskResult("max_steps", "adım sınırı doldu", steps)


def router_asker(router, role: str = "vision", effort: str = "low") -> Callable[[list[dict[str, Any]]], str]:
    # Düşük düşünme: aynı ekranda aynı kararı ~2x hızlı veriyor (ölçüm: 4,7 sn -> 2,4 sn).
    extra = {"reasoning_effort": effort} if effort else None

    def ask(messages: list[dict[str, Any]]) -> str:
        return router.chat(role, messages, temperature=0, max_tokens=2000, gemini_extra=extra).text

    return ask
