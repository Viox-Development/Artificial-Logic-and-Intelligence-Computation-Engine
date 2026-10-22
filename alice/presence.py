"""Bilgisayar başına dönünce karşılama (Jarvis'in "Hoş geldiniz efendim, bugün iki şey var" anı).

Çekirdek her turda klavye/fare boşta kalma süresine bakar. Uzun süre (varsayılan 90 dk) uzakta kalıp döndüğünde,
söyleyecek bir şey varsa kısa bir sesli brifing verir:
  * günü gelen takipler ("Matematik sınavın nasıl geçti?"), hatırlatmalar, yarım kalan iş
  * bugünün kalan takvimi (Google Takvim bağlıysa)
  * okunmamış önemli e-postalar (Gmail bağlıysa)
Söyleyecek bir şey yoksa sadece günün ilk dönüşünde kısa bir selam verir; yoksa susar. Metni hızlı model senin
kişiliğinle yazar (model yoksa kalıp cümle). Sonra birkaç saniye "Hey Alice" demeden cevap verebilirsin; konuşma
hafızaya da yazılır, Live "az önceki konuşma"da görür. Uçuş/oyun modu bildirimleri susturuyorsa karşılama da susar.

config.toml:
  [greeting]
  enabled = true
  away_minutes = 90      # bu kadar uzakta kalınca dönüş sayılır
  min_gap_hours = 3      # iki karşılama arası en az
  quiet_hours = "00:30-07:00"   # bu saatlerde karşılama yok
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable

DEFAULTS = {"enabled": True, "away_minutes": 90, "min_gap_hours": 3, "quiet_hours": "00:30-07:00"}


def idle_seconds() -> float | None:
    """Klavye/fare son kullanımdan beri geçen süre. Ölçülemiyorsa None."""
    try:
        if sys.platform == "win32":
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
                return None
            millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime  # type: ignore[attr-defined]
            return max(0.0, (millis & 0xFFFFFFFF) / 1000.0)
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "HIDIdleTime" in line:
                    return int(line.split("=")[-1].strip()) / 1e9
            return None
        out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip()) / 1000.0 if out.returncode == 0 else None
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return None


def settings(cfg) -> dict[str, Any]:
    return {**DEFAULTS, **((getattr(cfg, "data", None) or {}).get("greeting") or {})}


def part_of_day(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "Günaydın"
    if 12 <= h < 18:
        return "İyi günler"
    return "İyi akşamlar"


def _away_text(seconds: float) -> str:
    h = seconds / 3600
    if h >= 20:
        return "dünden beri"
    if h >= 1.5:
        return f"yaklaşık {round(h)} saattir"
    return f"{int(seconds // 60)} dakikadır"


# ------------------------------------------------------------------------------------------- brifing
def today_events(cfg, now: datetime, client: Any = None) -> list[str]:
    """Bugünün kalan etkinlikleri ("15:00 Dişçi"), Google Takvim bağlıysa."""
    try:
        from alice.connect import runner
        from alice.connect.manifest import discover

        m = discover(cfg)[0].get("google_calendar")
        if m is None or not runner.ready(m)[0]:
            return []
        start = now.astimezone()
        end = start.replace(hour=23, minute=59, second=0)
        data = runner.call(m, m.actions["upcoming"], {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                                                       "maxResults": 8}, client=client, cache=False)
    except Exception:  # noqa: BLE001 - takvim ipucu; ulaşılamazsa brifing yine çalışır
        return []
    out = []
    for ev in (data or {}).get("items") or []:
        st = (ev.get("start") or {}).get("dateTime") or ""
        when = st[11:16] if len(st) >= 16 else "gün boyu"
        if ev.get("summary"):
            out.append(f"{when} {ev['summary']}")
    return out


def unread_mail(cfg) -> list[str]:
    """Bağlı Gmail hesaplarındaki okunmamış önemli e-postalar ("okul: Hoca — Proje teslimi")."""
    try:
        from alice.tools.gmail import important_unread

        return important_unread(cfg, limit=5)
    except Exception:  # noqa: BLE001
        return []


def briefing(mem, cfg, away: float, now: datetime) -> dict[str, Any]:
    due = mem.due_followups(now.timestamp()) if mem is not None else []
    return {"greeting": part_of_day(now), "away": _away_text(away), "followups": due,
            "events": today_events(cfg, now), "mail": unread_mail(cfg)}


def has_news(b: dict[str, Any]) -> bool:
    return bool(b["followups"] or b["events"] or b["mail"])


COMPOSE_PROMPT = """Kullanıcı bilgisayarının başına döndü ({away} uzaktaydı). Onu sesli, kısa ve doğal bir şekilde \
karşıla. Kişiliğin aşağıda; o karakterle konuş. Kurallar: Türkçe, en fazla 3 kısa cümle, liste/markdown/emoji yok, \
saatleri okunur yaz ("üçte"). "{greeting}" gibi bir selamla başla (adını biliyorsan kullan). Takip edilecekleri doğal \
sor ya da hatırlat; birden çok şey varsa en önemlisini öne al, gerisini tek cümlede topla. Söylenecek bir şey yoksa \
sadece kısa bir selam ver. Uydurma: sadece aşağıdakileri kullan.

Kişilik (özet):
{persona}

Kullanıcı hakkında bilinenler:
{profile}

Takipler (sorulacak/hatırlatılacak/yarım kalan):
{followups}
Bugünün kalan takvimi:
{events}
Okunmamış önemli e-postalar:
{mail}"""


def compose(b: dict[str, Any], mem=None, router=None) -> str:
    fups = "\n".join(f"- ({f.kind}) {f.text}" for f in b["followups"]) or "(yok)"
    events = "\n".join(f"- {e}" for e in b["events"]) or "(yok)"
    mail = "\n".join(f"- {m}" for m in b["mail"]) or "(yok)"
    if router is not None:
        try:
            from alice.config import personality_text

            profile = mem.profile(chars=1200, recent=0) if mem is not None else ""
            prompt = COMPOSE_PROMPT.format(away=b["away"], greeting=b["greeting"], persona=personality_text()[:1500]
                                           or "(varsayılan: sakin, kısa, sıcak)", profile=profile[:1500] or "(yok)",
                                           followups=fups, events=events, mail=mail)
            res = router.chat("fast", [{"role": "user", "content": prompt}], temperature=0.7, max_tokens=220)
            text = (res.message.get("content") or "").strip()
            if text:
                return text
        except Exception:  # noqa: BLE001 - model yoksa kalıp cümle
            pass
    parts = [f"{b['greeting']}, hoş geldin."]
    if b["followups"]:
        parts.append(b["followups"][0].text)
    if b["events"]:
        parts.append(f"Bugün takviminde {len(b['events'])} şey var, ilki {b['events'][0]}.")
    if b["mail"]:
        parts.append(f"{len(b['mail'])} önemli e-postan var.")
    return " ".join(parts)


# ------------------------------------------------------------------------------------------- izleyici
class Greeter:
    """Çekirdeğin her turunda tick(); uzun yokluktan dönüşte karşılamayı arka planda hazırlayıp söyler."""

    def __init__(self, cfg, notify: Callable[[Any], bool], memory: Callable[[], Any] = lambda: None,
                 router: Callable[[], Any] = lambda: None, idle: Callable[[], float | None] = idle_seconds,
                 clock: Callable[[], datetime] = datetime.now):
        self.cfg = cfg
        self.notify = notify
        self.memory = memory
        self.router = router
        self.idle = idle
        self.clock = clock
        self.voice: Any = None  # VoiceAssistant: karşılamadan sonra "Hey Alice" demeden cevap verilebilsin
        self.away_since: float | None = None
        self.last_greet = 0.0
        self.last_day = ""
        self._busy = threading.Lock()

    def tick(self) -> bool:
        """Dönüş algılandıysa karşılamayı başlatır ve True döner."""
        s = settings(self.cfg)
        if not s.get("enabled", True):
            return False
        idle = self.idle()
        if idle is None:
            return False
        now = time.time()
        if idle >= float(s["away_minutes"]) * 60:
            if self.away_since is None:
                self.away_since = now - idle
            return False
        if self.away_since is None or idle > 60:
            return False
        away, self.away_since = now - self.away_since, None
        if not self._allowed(s, now):
            return False
        threading.Thread(target=self.greet, args=(away,), daemon=True, name="alice-greeting").start()
        return True

    def _allowed(self, s: dict[str, Any], now: float) -> bool:
        if now - self.last_greet < float(s["min_gap_hours"]) * 3600:
            return False
        from alice.core.modes import in_hours, parse_hours

        span = parse_hours(str(s.get("quiet_hours") or ""))
        return not (span and in_hours(self.clock(), span))

    def greet(self, away: float) -> str:
        if not self._busy.acquire(blocking=False):
            return ""
        try:
            now = self.clock()
            mem = self.memory()
            b = briefing(mem, self.cfg, away, now)
            first_today = self.last_day != now.strftime("%Y-%m-%d")
            if not has_news(b) and not first_today:
                return ""
            text = compose(b, mem, self.router())
            from alice.core.notify import Notification

            if not self.notify(Notification("Hoş geldin", text, "info", "presence", kind="info",
                                            emotion="cheerful")):
                return ""  # mod susturdu: sayma, sonra tekrar denenebilsin
            self.last_greet = time.time()
            self.last_day = now.strftime("%Y-%m-%d")
            self._after(mem, b, text)
            return text
        finally:
            self._busy.release()

    def _after(self, mem, b: dict[str, Any], text: str) -> None:
        if mem is not None:
            try:
                mem.record("(bilgisayarın başına döndü)", text, "karşılama")
                for f in b["followups"]:
                    if f.kind in ("ask", "remind"):
                        mem.done_followup(f.id)  # soruldu; cevap konuşmada gelir
            except Exception:  # noqa: BLE001
                pass
        va = self.voice
        if va is not None:
            try:
                va.speaker.wait(40)
                va.armed_until = max(getattr(va, "armed_until", 0.0), time.monotonic() + float(va.follow_up))
            except Exception:  # noqa: BLE001
                pass

