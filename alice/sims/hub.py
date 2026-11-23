"""Connects the simulators to the core: listeners -> flight companion -> notification (and sound).

Runs in the same process as the core. The voice assistant is in that process too, so the `flight` tool
reaches the live state through ACTIVE.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from alice.core.notify import Notification, Notifier
from alice.core.store import Store
from alice.sims.flight import Debrief, FlightCompanion, TouchdownTracker, save_debrief

ACTIVE: "SimHub | None" = None


class SimHub:
    def __init__(self, cfg: dict[str, Any], store: Store, notifier: Notifier, processes=lambda: set(), show=None):
        self.cfg = cfg
        self.show = show  # Draws on the HUD (alice.hud.client.show); if None, sound only
        self.store = store
        self.notifier = notifier
        self.processes = processes
        self.companions: dict[str, FlightCompanion] = {}
        self.last_debrief: tuple[Debrief, str] | None = None
        self._parts: list = []
        self._briefed: str = ""  # Mission already briefed (path + modification time): once per mission
        self._fullscreen_said = False
        self._coach_said: dict[str, str] = {}  # Pattern -> day it was said
        self._coach_last = 0.0
        self._break_last = 0.0

    def _fullscreen_check(self, game: str) -> None:
        """HUD on and the game in exclusive fullscreen: the arrows are invisible; say so once while the core runs."""
        if self.show is None or self._fullscreen_said:
            return
        from alice.hud.screen import exclusive_fullscreen

        if exclusive_fullscreen():
            self._fullscreen_said = True
            self._say(f"{game} özel tam ekranda, HUD oyunun üstüne çizemiyor. Görüntü ayarlarından kenarlıksız "
                      "pencereye geçersen tehdit oklarını da görürsün.", "info", "calm")

    def _say(self, text: str, severity: str, emotion: str) -> None:
        self.notifier.send(Notification("Uçuş", text, severity, "flight", kind="flight", emotion=emotion))

    def _hr_stats(self, a: float, b: float) -> dict | None:
        s = self.store.stats("watch.hr", a, b)
        return {"avg": s.avg, "max": s.max, "min": s.min} if s.count >= 3 else None

    def _hr_now(self) -> float | None:
        last = self.store.last("watch.hr")
        return last[1] if last and time.time() - last[0] < 60 else None

    def _debrief(self, d: Debrief) -> None:
        page = save_debrief(d, self.store.series("watch.hr", d.start_wall, d.end_wall))
        self.last_debrief = (d, str(page))
        self.store.add_event("flight", f"Uçuş değerlendirmesi: {d.aircraft}", f"{d.summary()} ({page})", "info", "flight")
        try:  # So the next flight's bingo also uses this flight's cruise fuel
            from alice.sims.flight import learn_cruise

            learn_cruise(page.parent)
        except Exception:  # noqa: BLE001 - learning must not break the debrief
            pass

    def companion(self, source: str) -> FlightCompanion:
        if source not in self.companions:
            self.companions[source] = FlightCompanion(self._say, self._hr_stats, self._hr_now, self._debrief,
                                                        show=self.show)
        return self.companions[source]

    def start(self) -> "SimHub":
        global ACTIVE
        if self.cfg.get("dcs", True):
            from alice.sims.dcs import PORT, DCSListener, ensure_installed
            from alice.sims.wingmen import settings as wingmen_settings

            try:  # Install the script if it is missing or outdated: DCS sends no data without it
                wport, wnames = wingmen_settings(self.cfg)
                for d in ensure_installed(port=int(self.cfg.get("dcs_port", PORT)), wingmen_port=wport,
                                          wingmen=wnames):
                    self.store.add_event("info", "DCS veri betiği kuruldu", f"{d} (DCS'i yeniden başlat)", "info",
                                         "flight")
            except OSError as e:
                self.store.add_event("error", "DCS veri betiği kurulamadı", str(e), "warn", "flight")

            comp = self.companion("dcs")
            tracker = TouchdownTracker(comp)

            def feed(s, comp=comp, tracker=tracker):
                if comp.last_state is None:
                    self._fullscreen_check("DCS")
                    if (s.ias or 0) < 30:  # New session on the ground: brief before takeoff
                        threading.Thread(target=self.brief_mission, daemon=True, name="alice-dcs-brief").start()
                tracker.feed(s)

            try:
                self._parts.append(DCSListener(feed, comp.restricted, lambda c=comp: self._session_end(c),
                                               lambda rows: self.store.add_many(rows),
                                               port=int(self.cfg.get("dcs_port", PORT))).start())
            except OSError as e:
                self.store.add_event("error", "DCS dinleyicisi başlamadı", str(e), "warn", "flight")
        if self.cfg.get("warthunder", True):
            from alice.sims.warthunder import WarThunderPoller

            comp = self.companion("warthunder")
            tracker = TouchdownTracker(comp)
            running = lambda: any(p.lower() == "aces.exe" for p in self.processes())  # noqa: E731

            vision = self._missile_vision()

            def wt_feed(s, comp=comp, tracker=tracker):
                if comp.last_state is None:  # New session (match)
                    self._fullscreen_check("War Thunder")
                if vision is not None:  # A missile warning seen on screen goes into the contact list
                    vision.poke()
                    seen = vision.contacts()
                    if seen:
                        s.extra["contacts"] = list(s.extra.get("contacts") or []) + seen
                tracker.feed(s)

            self._parts.append(WarThunderPoller(wt_feed, lambda c=comp: self._session_end(c),
                                                lambda rows: self.store.add_many(rows), running=running,
                                                nick=str(self.cfg.get("warthunder_nick") or "") or None).start())
        ACTIVE = self
        return self

    COACH_GAP = 20 * 60.0   # Minimum gap between two coaching sentences (s)
    BREAK_GAP = 45 * 60.0   # "ara ver" (take a break) suggestion, at most this often

    def _session_end(self, comp: FlightCompanion) -> None:
        """Death / end of match: close the flight, then check death patterns and mood in the background."""
        comp.session_end()
        threading.Thread(target=self._after_death, args=(comp,), daemon=True, name="alice-coach").start()

    def _after_death(self, comp: FlightCompanion, now: float | None = None) -> None:
        from datetime import date

        from alice import mood
        from alice.sims import coach

        now = time.time() if now is None else now
        try:
            flights = coach.load_flights(limit=40)
            events = self.store.events(since=now - 6 * 3600, kind="flight", limit=5000)
            found = coach.patterns(coach.contexts(flights, events))
            m = mood.current(self.store, now, flights=flights)
        except Exception:  # noqa: BLE001 - coaching must not break the flight
            return
        comp.coach_flags = set(found)
        comp.calm = m.level == "tilt"
        if m.level == "tilt" and now - self._break_last >= self.BREAK_GAP:
            self._break_last = now
            self._say("Üst üste düştün. Bir maç ara vermek iyi gelebilir.", "info", "calm")
            return
        today = date.fromtimestamp(now).isoformat()
        for key, text in found.items():
            if self._coach_said.get(key) != today and now - self._coach_last >= self.COACH_GAP:
                self._coach_said[key], self._coach_last = today, now
                self._say(text, "info", "calm")
                break

    def _missile_vision(self):
        """Detect missiles on screen if [sims] missile_vision is on and the RWR region is set (alice/sims/vision.py)."""
        if not self.cfg.get("missile_vision"):
            return None
        from alice.sims.vision import MissileWatcher, region_of

        region = region_of(self.cfg)
        if region is None:  # If not set, War Thunder's default RWR location (scaled to screen height)
            from alice.sims.vision import default_region

            try:
                from alice.tools.imaging import screen_capture

                with screen_capture() as sct:
                    region = default_region(int(sct.monitors[1]["height"]))
            except Exception:  # noqa: BLE001
                region = default_region(1080)
        w = MissileWatcher(region, self.cfg.get("vision") or {})
        self._parts.append(w)
        return w

    def stop(self) -> None:
        global ACTIVE
        for p in self._parts:
            p.stop()
        if ACTIVE is self:
            ACTIVE = None

    def brief_mission(self, path=None) -> str:
        """Read the loaded DCS mission: give a short spoken briefing and update the kneeboard. Once per mission."""
        from alice.sims import kneeboard as kb
        from alice.sims.dcs import briefing_text, current_mission, read_mission, saved_games_dirs

        try:
            path = path or current_mission()
            if path is None:
                return ""
            key = f"{path}|{path.stat().st_mtime}"
            if key == self._briefed:
                return ""
            self._briefed = key
            info = read_mission(path)
        except (OSError, ValueError, KeyError) as e:
            self.store.add_event("info", "DCS görevi okunamadı", str(e)[:200], "info", "flight")
            return ""
        text = briefing_text(info)
        self._say(text, "info", "calm")
        try:
            dirs = saved_games_dirs()
            if dirs:
                kb.write(kb.render(info, kb.load_notes()), kb.kneeboard_dirs(dirs))
        except Exception:  # noqa: BLE001 - the briefing was spoken even without the kneeboard
            pass
        return text

    def cockpit(self, command: str, mhz: float | None = None, wait: float = 2.0) -> str:
        """Voice cockpit command (DCS only; allow-list in the script). The gear is not raised on the ground;
        not sent if already in position. Waits for the script's "ack"; if none comes, the script may be outdated."""
        from alice.sims.dcs import COMMANDS, send_command

        c = self.companions.get("dcs")
        s = c.last_state if c is not None else None
        if s is None or time.time() - s.wall > 5:
            return "yapılamadı: DCS'ten canlı veri gelmiyor (sesli kokpit komutları sadece DCS'te)"
        if command == "radio":
            if not mhz:
                return "yapılamadı: frekans (MHz) gerekli"
            seq = send_command("freq", f"{float(mhz):.3f}")
        else:
            name = command.removesuffix("_up").removesuffix("_down")
            if name not in COMMANDS:
                return f"yapılamadı: bilinmeyen komut {command}"
            if name == "gear":
                down = (s.gear or 0) > 0.5
                if command == "gear_up" and c.phase != "air":
                    return "yapmadım: yerdeyken teker kaldırılmaz"
                if (command == "gear_down") == down:
                    return f"gerek yok: teker zaten {'aşağıda' if down else 'yukarıda'}"
            seq = send_command("cmd", name)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            ack = str((c.last_state.extra or {}).get("ack") or "")
            if ack.startswith(f"{seq}|"):
                _, status, *rest = ack.split("|")
                return ("yapıldı: " if status == "ok" else "yapılamadı: ") + "|".join(rest)
            time.sleep(0.1)
        return ("gönderildi ama DCS onay vermedi: veri betiği eski olabilir (DCS'i yeniden başlat) ya da bu modül "
                "komutu desteklemiyor")

    def alert_home(self, nm: float) -> str:
        """Set an alert on the airborne flight: 'üsse nm mil kalınca söyle' (tell me when nm miles from home)."""
        nm = float(nm)
        for c in self.companions.values():
            if c.last_state is not None and c.phase == "air":
                if c.home is None:
                    return "kurulamadı: üssün konumu bilinmiyor (kalkış kaydedilmedi)"
                c.home_alert_nm = float(nm)
                return f"kuruldu: üsse {nm:.0f} mil kalınca sesli söylenecek"
        return "kurulamadı: şu an uçuşta değilsin"

    def _flying(self):
        return next((c for c in self.companions.values() if c.last_state is not None and c.phase == "air"), None)

    def alert_altitude(self, ft: float) -> str:
        """'X feet'e gelince söyle' (tell me when reaching X feet): say it once, whichever direction is crossed."""
        ft = float(ft)
        c = self._flying()
        if c is None:
            return "kurulamadı: şu an uçuşta değilsin"
        c.alt_alert_ft, c.alt_alert_above = float(ft), None
        return f"kuruldu: {ft:,.0f} feet'e gelince sesli söylenecek".replace(",", ".")

    def alert_fuel(self, kg: float) -> str:
        kg = float(kg)
        c = self._flying()
        if c is None:
            return "kurulamadı: şu an uçuşta değilsin"
        s = c.last_state
        if s.fuel_kg is None:
            return "kurulamadı: oyun yakıt miktarını vermiyor"
        if s.fuel_kg <= kg:
            return f"kurulmadı: yakıt zaten {s.fuel_kg:.0f} kg, {kg:.0f} kg'nin altında"
        c.fuel_alert_kg = float(kg)
        return f"kuruldu: yakıt {kg:.0f} kg'ye inince sesli söylenecek"

    def clear_alerts(self) -> str:
        n = 0
        for c in self.companions.values():
            n += sum(v is not None for v in (c.home_alert_nm, c.alt_alert_ft, c.fuel_alert_kg))
            c.home_alert_nm = c.alt_alert_ft = c.alt_alert_above = c.fuel_alert_kg = None
        return f"{n} uyarı kaldırıldı" if n else "kurulu uyarı yoktu"

    def status(self) -> str:
        lines = []
        for src, c in self.companions.items():
            s = c.last_state
            if s is None:
                continue
            if c.phase != "air":
                lines.append(f"{src}: {c.profile.name}, yerde")
                continue
            lines.append(f"{src}: {c.profile.name}, {(s.alt or 0) / 0.3048:,.0f} ft, {(s.ias or 0) / 0.514444:.0f} kt, "
                         f"{s.g or 0:.1f} G")
            fs = c.fuel_status(s)
            if fs:
                extra = (f", üs {fs['home_nm']:.0f} nm, bingo {fs['bingo_kg']:.0f} kg "
                         f"({fs['margin_kg']:+.0f} kg pay)" if "bingo_kg" in fs else "")
                lines.append(f"  yakıt {s.fuel_kg:.0f} kg, tüketim {fs['burn_kgs'] * 3600:.0f} kg/sa, "
                             f"{fs['left_min']:.0f} dk kaldı{extra}")
            nav = c.home_nav(s)
            if nav:
                turn = ""
                if "turn" in nav:
                    t = nav["turn"]
                    turn = (" (burnun zaten üsse dönük)" if abs(t) < 10 else
                            f", şu an burnun {s.heading:03.0f}°: {abs(t):.0f}° {'sağa' if t > 0 else 'sola'} dön")
                lines.append(f"  üs (kalkış noktası): {nav['bearing']:03.0f}° yönünde (gerçek kuzey), "
                             f"{nav['nm']:.0f} nm{turn}")
            elif s.source == "dcs":
                lines.append("  üsse yön bilinmiyor (kalkış noktası kaydedilmedi)")
            from alice.sims.flight import describe_extra

            lines += describe_extra(s)
            if "weapons" not in s.extra:
                lines.append("  silah/mühimmat: " + ("War Thunder bu bilgiyi yayınlamıyor, BİLİNMİYOR" if src == "warthunder"
                                                     else "oyun bu uçak için vermedi, BİLİNMİYOR"))
            if c.home_alert_nm is not None:
                lines.append(f"  kurulu uyarı: üsse {c.home_alert_nm:.0f} mil kalınca söylenecek")
            if c.alt_alert_ft is not None:
                lines.append(f"  kurulu uyarı: {c.alt_alert_ft:.0f} ft'e gelince söylenecek")
            if c.fuel_alert_kg is not None:
                lines.append(f"  kurulu uyarı: yakıt {c.fuel_alert_kg:.0f} kg'ye inince söylenecek")
            if s.chaff is not None:
                lines.append(f"  chaff {s.chaff}, flare {s.flare}")
            cs = s.extra.get("contacts")
            if isinstance(cs, list):
                planes = [f"saat {c['clock']} {c['dist_m'] / 1000:.1f} km" for c in cs if c.get("kind") == "aircraft"]
                radars = [f"{c['name']} saat {c['clock']}" + (" (FÜZE)" if c.get("missile") else " (kilit)"
                                                               if c.get("lock") else "")
                          for c in cs if c.get("kind") == "radar"]
                if planes:
                    lines.append("  en yakın düşmanlar (mini haritada, irtifa bilinmiyor): " + ", ".join(planes[:4]))
                if radars:
                    lines.append("  RWR: " + ", ".join(radars[:6]))
                if not planes and not radars:
                    lines.append("  görünen düşman/radar yok")
        if lines:
            return "\n".join(lines)
        if self.cfg.get("dcs", True) and "dcs" in self.companions and self.companions["dcs"].last_state is None:
            return ("DCS'ten bu oturumda hiç veri gelmedi. DCS açıksa görevi yeniden başlatmak gerekir (veri betiği "
                    "görev açılırken yüklenir); olmazsa `alice dcs status` ile kontrol et.")
        return "şu an uçuşta değilsin"
