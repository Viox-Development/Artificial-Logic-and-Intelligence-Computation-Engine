"""Flight tool (one tool, light on context): live status, last debrief, checklists, kneeboard."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from alice.tools.base import READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj

ACTIONS = ["status", "cockpit", "brief", "alert_home", "alert_alt", "alert_fuel", "alerts_clear", "last_debrief", "checklist_list", "checklist_start", "checklist_next", "checklist_prev",
           "checklist_stop", "kneeboard_build", "kneeboard_note", "kneeboard_clear", "kneeboard_show"]


class Flight(Tool):
    name = "flight"
    description = ("DCS / War Thunder uçuşu: canlı durum, sesli uyarılar, kontrol listeleri, kneeboard.\n"
                   "- Hız, irtifa, yakıt, yön, üsse mesafe, silah sorulunca HER SEFER status çağır; eski cevabı "
                   "tekrarlama.\n"
                   "- status'ta olmayan ya da BİLİNMİYOR yazan bilgiyi ASLA uydurma ve genel bilginden tahmin etme; "
                   "'oyun bu bilgiyi vermiyor' de.\n"
                   "- Sadece alert_home/alt/fuel ile kurulabilen uyarıları söz ver ('X mile gelince söyle' -> "
                   "alert_home).\n"
                   "- 'Teker indir', 'flare at', 'telsizi 251'e al' gibi istekler -> cockpit (sadece DCS).\n"
                   "- Kontrol listesinde sesliyken sadece dönen adımı oku; 'tamam/devam' gelince checklist_next.")
    parameters = obj({
        "action": {"type": "string", "enum": ACTIONS,
                   "description": "status: canlı yakıt, kalan dakika, üssün yönü/mesafesi, kaç derece dönmeli, en yakın "
                   "düşmanlar/RWR | cockpit: DCS'te command'ı uygula | brief: yüklü DCS görevinin brifingini oku | "
                   "alert_home: üsse nm mil kalınca söyle | alert_alt: ft irtifasına gelince söyle | alert_fuel: yakıt "
                   "kg'a inince söyle | alerts_clear | last_debrief: son uçuşun değerlendirmesi | checklist_list | "
                   "checklist_start: name listesini başlat | checklist_next/prev/stop | kneeboard_note: text'i not "
                   "et | kneeboard_build: görevden kneeboard yap | kneeboard_show | kneeboard_clear."},
        "name": {"type": "string", "description": "checklist_start için liste adı: kalkış, kalkış sonrası, iniş, iniş "
                 "sonrası, motor çalıştırma, motor arızası, savaş girişi, savaş çıkışı."},
        "text": {"type": "string", "description": "kneeboard_note için not."},
        "mission": {"type": "string", "description": "kneeboard_build için .miz yolu. Boş: son görev."},
        "nm": {"type": "number", "description": "alert_home için deniz mili."},
        "ft": {"type": "number", "description": "alert_alt için feet."},
        "kg": {"type": "number", "description": "alert_fuel için kilogram."},
        "command": {"type": "string", "enum": ["gear_up", "gear_down", "flaps", "airbrake", "hook", "flare", "chaff",
                                               "radio"],
                    "description": "cockpit için. flaps/airbrake/hook aç-kapa yapar; radio: mhz frekansına ayarla."},
        "mhz": {"type": "number", "description": "cockpit radio için frekans, ör. 251 ya da 124.5."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action", "")
        return WRITE if a in ("kneeboard_build", "kneeboard_note", "kneeboard_clear") else READ

    def run(self, ctx: ToolContext, action: str, name: str = "", text: str = "", mission: str = "",
            nm: float | None = None, ft: float | None = None, kg: float | None = None, command: str = "",
            mhz: float | None = None) -> ToolResult:
        if action in ("cockpit", "brief"):
            from alice.sims import hub

            if hub.ACTIVE is None:
                return ToolResult("uçuş takibi bu süreçte çalışmıyor (alice core run ile başlar)")
            if action == "brief":
                hub.ACTIVE._briefed = ""
                return ToolResult(hub.ACTIVE.brief_mission() or "yüklü görev bulunamadı")
            if not command:
                raise ToolError("command gerekli")
            return ToolResult(hub.ACTIVE.cockpit(command, _number(mhz)))
        if action.startswith("alert"):
            from alice.sims import hub

            if hub.ACTIVE is None:
                return ToolResult("uçuş takibi bu süreçte çalışmıyor (alice core run ile başlar)")
            if action == "alerts_clear":
                return ToolResult(hub.ACTIVE.clear_alerts())
            name, value, call = {"alert_home": ("nm", nm, hub.ACTIVE.alert_home),
                                 "alert_alt": ("ft", ft, hub.ACTIVE.alert_altitude),
                                 "alert_fuel": ("kg", kg, hub.ACTIVE.alert_fuel)}[action]
            value = _number(value)
            if not value or value <= 0:
                raise ToolError(f"{name} gerekli")
            return ToolResult(call(value))
        if action.startswith("checklist_"):
            return self._checklist(ctx, action[len("checklist_"):], name)
        if action.startswith("kneeboard_"):
            return self._kneeboard(action[len("kneeboard_"):], mission, text)
        return self._status(action)

    # ----------------------------------------------------------- status
    @staticmethod
    def _status(action: str) -> ToolResult:
        from alice.sims import hub
        from alice.sims.flight import Debrief, flights_dir

        if action == "status":
            if hub.ACTIVE is None:
                return ToolResult("uçuş takibi bu süreçte çalışmıyor (alice core run ile başlar)")
            return ToolResult(hub.ACTIVE.status())
        if hub.ACTIVE is not None and hub.ACTIVE.last_debrief:
            d, page = hub.ACTIVE.last_debrief
            return ToolResult(f"{d.summary()}\nRapor: {page}")
        files = sorted(flights_dir().glob("*.json")) if flights_dir().is_dir() else []
        if not files:
            return ToolResult("kayıtlı uçuş yok")
        d = Debrief(**json.loads(files[-1].read_text(encoding="utf-8")))
        return ToolResult(f"{d.summary()}\nRapor: {files[-1].with_suffix('.html')}")

    # -------------------------------------------------- checklists
    @staticmethod
    def _checklist(ctx: ToolContext, action: str, name: str) -> ToolResult:
        from alice.sims.checklists import Runner, find, load_all

        if action == "list":
            return ToolResult("\n".join(f"{cl.key}: {cl.title} ({len(cl.steps)} adım)" for cl in load_all().values())
                              or "kontrol listesi yok")
        if action == "start":
            cl = find(name)
            if cl is None:
                raise ToolError(f"liste bulunamadı: {name}")
            ctx.state["checklist"] = Runner(cl)
            return ToolResult(f"{cl.title}. {ctx.state['checklist'].current()}")
        run = ctx.state.get("checklist")
        if run is None:
            raise ToolError("açık kontrol listesi yok (önce checklist_start)")
        if action == "stop":
            ctx.state.pop("checklist")
            return ToolResult("kontrol listesi kapatıldı")
        text = {"next": run.next, "prev": run.prev}[action]()
        if run.done:
            ctx.state.pop("checklist")
        return ToolResult(text)

    # -------------------------------------------------------- kneeboard
    @staticmethod
    def _kneeboard(action: str, mission: str, text: str) -> ToolResult:
        from alice.sims import kneeboard as kb
        from alice.sims.dcs import latest_mission, read_mission, saved_games_dirs

        if action == "show":
            return ToolResult("\n".join(f"{n['time']} {n['text']}" for n in kb.load_notes()) or "not yok")
        if action == "note":
            if not text:
                raise ToolError("text gerekli")
            kb.add_note(text)
        elif action == "clear":
            kb.clear_notes()
        dirs = saved_games_dirs()
        if not dirs:
            raise ToolError("DCS Saved Games klasörü bulunamadı")
        info = None
        path = Path(mission).expanduser() if mission else latest_mission(dirs)
        if path is not None and path.exists():
            try:
                info = read_mission(path)
            except (ValueError, KeyError, OSError) as e:
                if action == "build":
                    raise ToolError(f"görev okunamadı: {e}") from e
        written = kb.write(kb.render(info, kb.load_notes()), kb.kneeboard_dirs(dirs))
        what = f"görev: {info.name}, {len(info.waypoints)} waypoint" if info else "görev yok"
        return ToolResult(f"kneeboard güncellendi ({len(written)} sayfa; {what}).")


class DcsWingman(Tool):
    name = "dcs_wingman"
    description = ("DCS tek oyunculu görevde dost yapay zekâ kanat adamlarına sesli emir: saldır/bastır, yanıma gel, "
                   "bekle (tur at), üsse dön. Ör. 'İkinci, sağdaki SAM'i bastır', 'Üçüncü eve dön', 'hepsi toplan'.\n"
                   "- Kullanıcının sözünü olduğu gibi command'a koy; ALICE çözer. Ya da verb/index/target/direction "
                   "alanlarını doldur.\n"
                   "- Sıra: oyuncu lider (1); İkinci = en yakın kanat adamı, Üçüncü = sonraki, 0 = hepsi.\n"
                   "- Sonuç oyunda ekranda yazı olarak görünür (ör. 'İkinci: SAM'e saldırıyor'); burada sadece "
                   "gönderildiği söylenir. DCS kapalıysa ya da çok oyunculuda başkasının sunucusundaysan hiçbir şey "
                   "olmaz.")
    parameters = obj({
        "command": {"type": "string", "description": "Kullanıcının sözü, ör. 'İkinci, sağdaki SAM'i bastır'. "
                    "Verilirse diğer alanlar boşsa buradan çıkarılır."},
        "verb": {"type": "string", "enum": ["attack", "rejoin", "hold", "rtb"],
                 "description": "attack: hedefe saldır | rejoin: oyuncuyu takip et | hold: bulunduğu yerde tur at | "
                 "rtb: en yakın dost üsse in."},
        "index": {"type": "integer", "description": "Kanat adamı sırası: 2 = İkinci (en yakın), 3 = Üçüncü, 0 = hepsi."},
        "target": {"type": "string", "enum": ["sam", "air", "ground", "ship", "any"],
                   "description": "attack için hedef türü: sam (hava savunma), air (uçak), ground (kara), ship, any."},
        "direction": {"type": "string", "enum": ["right", "left", "front", "behind", "any"],
                      "description": "attack için hedefin oyuncunun burnuna göre yönü."},
    })
    risk = READ

    def run(self, ctx: ToolContext, command: str = "", verb: str = "", index: Any = None, target: str = "",
            direction: str = "") -> ToolResult:
        from alice.sims import wingmen as wm

        cmd = wm.parse(command) if command else None
        if cmd is None:
            if not verb:
                raise ToolError("komut anlaşılmadı: verb (attack/rejoin/hold/rtb) ver ya da command'ı netleştir")
            cmd = wm.Command(verb)
        if verb:
            cmd.verb = verb
        n = _number(index)
        if n is not None:
            cmd.index = int(n)
        cmd.target = target or cmd.target
        cmd.direction = direction or cmd.direction
        port, _ = wm.settings((ctx.config.data.get("sims", {}) if ctx and ctx.config else {}) or {})
        try:
            ok = wm.send(cmd, port)
        except ValueError as e:
            raise ToolError(str(e)) from e
        if not ok:
            return ToolResult(f"gönderilemedi ({cmd.describe()}): yerel UDP hatası")
        return ToolResult(f"gönderildi: {cmd.describe()}. Sonuç DCS'te ekranda yazı olarak görünür.")


TOOLS = [Flight(), DcsWingman()]


def _number(value: Any) -> float | None:
    """The model sometimes sends a number as text ("10", "12,5", "10 mil" = 10 miles); convert it, else None."""
    if value is None or isinstance(value, (int, float)):
        return value
    text = str(value)
    m = re.search(r"-?\d{1,3}(?:([.,])\d{3})(?:\1\d{3})*(?![.,]?\d)", text)  # "5.000", "12,000" (thousands separators)
    if m:
        return float(re.sub(r"[.,]", "", m.group()))
    m = re.search(r"-?\d+(?:[.,]\d+)?", text)
    return float(m.group().replace(",", ".")) if m else None
