"""Ayarlar → Kurulum: kalan komut satırı işleri tek yerde.

* Sağlayıcıları dene (`alice doctor`): her sağlayıcıya kısa bir istek, karar motoru, özelliklerin durumu.
* Modelleri yeniden ölç (`alice models --refresh`) ve rolleri otomatiğe al (`--write`).
* Google kurulumu (`alice setup google`): adımlar sayfalarıyla, indirilen JSON'u bulup anahtarları yazma.
* Telefon (`alice phone`): aç/kapat, adres ve QR kod, yeni anahtar.
* DCS (`alice dcs`): dışa aktarma betiğini kur/kaldır, kneeboard sayfalarını yaz.
* Eklentiler (`alice plugins`): liste, hatalar, yeni eklenti şablonu.
* Masaüstü kısayolu (`alice shortcut`).
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from alice.app.automations import open_file


class Job:
    """Arka planda süren tek iş: durum, ilerleme satırları ve sonuç. Uygulama view()'i sık sık okur."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.s: dict[str, Any] = {"state": "idle", "lines": [], "result": None, "error": ""}

    def view(self) -> dict[str, Any]:
        with self._lock:
            return {**self.s, "lines": list(self.s["lines"][-12:])}

    def say(self, line: str) -> None:
        with self._lock:
            self.s["lines"].append(line)

    def start(self, fn: Callable[["Job"], Any]) -> dict[str, Any]:
        if self._thread is not None and self._thread.is_alive():
            return {"ok": False, "error": "zaten sürüyor"}
        with self._lock:
            self.s = {"state": "running", "lines": [], "result": None, "error": "", "started": time.time()}

        def run() -> None:
            try:
                result = fn(self)
                with self._lock:
                    self.s.update(state="done", result=result)
            except Exception as e:  # noqa: BLE001 - ekranda göster
                with self._lock:
                    self.s.update(state="error", error=str(e) or type(e).__name__)

        self._thread = threading.Thread(target=run, daemon=True, name="alice-setup-job")
        self._thread.start()
        return {"ok": True}


def qr_svg(text: str) -> str:
    """Telefonla okutulacak adres için SVG QR (qrcode paketi yoksa boş)."""
    try:
        import qrcode
    except ImportError:
        return ""
    qr = qrcode.QRCode(border=2)
    qr.add_data(text)
    qr.make()
    m = qr.get_matrix()
    n = len(m)
    rects = "".join(f'<rect x="{x}" y="{y}" width="1" height="1"/>' for y, row in enumerate(m)
                    for x, on in enumerate(row) if on)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><g fill="#000">{rects}</g></svg>')


class SetupPanel:
    def __init__(self, cfg: Any, router: Any, system: Any = None):
        self.cfg, self.router, self.system = cfg, router, system
        self.doctor = Job()
        self.models = Job()

    # ------------------------------------------------------------------ görünüm
    def view(self) -> dict[str, Any]:
        from alice import plugins
        from alice.providers import ranking
        from alice.setup_google import STEPS, find_downloaded_json

        cache = ranking.load_cache()
        found = find_downloaded_json()
        return {
            "doctor": self.doctor.view(),
            "models": {**self.models.view(), "probed_at": cache.get("probed_at", ""),
                       "chains": cache.get("chains") or {},
                       "auto": all("auto" in (self.cfg.roles.get(r) or []) for r in ("brain", "fast", "vision"))},
            "google": {"steps": [{"title": t, "url": u, "how": h} for t, u, h in STEPS],
                       "client": bool(os.environ.get("GOOGLE_CLIENT_ID", "").strip()),
                       "found_json": found.name if found else ""},
            "phone": self._phone(),
            "dcs": self._dcs(),
            "plugins": {"dir": str(plugins.plugin_dir()), "errors": plugins.errors(),
                        "items": [{"name": p.name, "version": p.version, "tools": sorted(p.tool_names()),
                                   "mode": "her zaman" if p.always else "gerekince", "source": p.source}
                                  for p in plugins.active()]},
            "windows": os.name == "nt",
        }

    def _phone(self) -> dict[str, Any]:
        from alice.app.phone import lan_ip, token

        p = self.cfg.data.get("phone", {})
        out: dict[str, Any] = {"enabled": bool(p.get("enabled")), "url": "", "qr": "",
                               "qr_lib": importlib.util.find_spec("qrcode") is not None}
        if out["enabled"]:
            url = f"http://{lan_ip()}:{int(p.get('port', 8765))}/quick?t={token()}"
            out.update(url=url, qr=qr_svg(url))
        return out

    @staticmethod
    def _dcs() -> dict[str, Any]:
        from alice.sims import dcs

        dirs = dcs.saved_games_dirs()
        m = dcs.latest_mission(dirs) if dirs else None
        return {"dirs": [{"path": str(d), "installed": (d / "Scripts" / "alice_export.lua").exists()} for d in dirs],
                "mission": m.name if m else ""}

    # ------------------------------------------------------------------ işlemler
    ACTIONS = {"weights_set", "doctor_run", "models_refresh", "models_auto", "google_open", "google_client", "phone_enable",
               "phone_new_token", "dcs_install", "dcs_kneeboard", "plugin_new", "plugin_folder", "shortcut"}

    def action(self, action: str, d: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self, "_" + action) if action in self.ACTIONS else None
        if fn is None:
            return {"ok": False, "error": f"bilinmeyen işlem: {action}"}
        try:
            return fn(d)
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}

    def _doctor_run(self, d: dict[str, Any]) -> dict[str, Any]:
        return self.doctor.start(self._run_doctor)

    def _run_doctor(self, job: Job) -> dict[str, Any]:
        from alice.decider import Choice, Decider, DeciderError, Noul
        from alice.doctor import probe_providers, run_all
        from alice.providers.router import Router

        providers = probe_providers(self.cfg, emit=job.say)
        router = Router(self.cfg, max_wait=0)
        job.say("karar motoru deneniyor…")
        decider = Decider.from_config(self.cfg, router)
        try:
            dec = decider.decide("A login form is visible with username, password fields and a 'Sign in' button. "
                                 "Goal: sign in.",
                                 {"next": Choice("Next action?", {"type_user": "Type username",
                                                                  "click_signin": "Click Sign in", "scroll": "Scroll"}),
                                  "done": Noul("Is the user already signed in?")})
            decider_line = (f"{', '.join(decider.names)}: next={dec['next'].value} ({dec['next'].confidence:.2f}), "
                            f"{dec.latency * 1000:.0f} ms, {dec.engine}")
            decider_ok = True
        except DeciderError as e:
            decider_line, decider_ok = f"çalışmıyor: {e}", False
        job.say("özellikler kontrol ediliyor…")
        checks = [c.__dict__ for c in run_all(self.cfg, Path.home())]
        return {"providers": providers, "decider": decider_line, "decider_ok": decider_ok, "checks": checks}

    def _models_refresh(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.providers import ranking

        def run(job: Job) -> dict[str, Any]:
            cache = ranking.refresh(self.router, emit=job.say)
            self.router.reload_auto(cache)
            return {"chains": cache.get("chains") or {}, "probed_at": cache.get("probed_at", ""),
                    "working": sum(1 for r in (cache.get("results") or {}).values() if r.get("ok")),
                    "total": len(cache.get("results") or {})}

        return self.models.start(run)

    def scores_view(self) -> dict[str, Any]:
        from alice.providers import ranking

        cache = ranking.load_cache()
        weights = ranking.load_weights()
        return {"metrics": ranking.METRICS, "weights": weights, "defaults": ranking.DEFAULT_WEIGHTS,
                "rows": ranking.score_table(cache, weights, self.router.providers_cfg), "chains": cache.get("chains") or {},
                "probed_at": cache.get("probed_at", ""), "state": self.models.view()["state"]}

    def _weights_set(self, d: dict[str, Any]) -> dict[str, Any]:
        """Bir rolün ağırlıklarını kaydet (ya da varsayılana dön) ve zincirleri son ölçümden yeniden kur."""
        from alice.providers import ranking

        weights = ranking.load_weights()
        role = str(d.get("role", ""))
        if d.get("reset"):
            weights = {r: dict(w) for r, w in ranking.DEFAULT_WEIGHTS.items()} if not role else \
                {**weights, role: dict(ranking.DEFAULT_WEIGHTS[role])}
        else:
            if role not in ranking.DEFAULT_WEIGHTS:
                return {"ok": False, "error": f"bilinmeyen rol: {role}"}
            new = d.get("weights") or {}
            vals = {k: float(new.get(k, weights[role][k])) for k in ranking.METRICS}
            if any(not 0 <= v <= 100 for v in vals.values()) or not sum(vals.values()):
                return {"ok": False, "error": "ağırlıklar 0-100 arası olmalı, hepsi sıfır olamaz"}
            weights[role] = vals
        ranking.save_weights(weights)
        ranking.rebuild(self.router, weights)
        return {"ok": True, **self.scores_view()}

    def _models_auto(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.cli import write_roles

        chains = {"brain": ["auto", "ollama:qwen3:8b"], "fast": ["auto", "ollama:qwen3:8b"],
                  "vision": ["auto", "gemini:gemini-flash-lite-latest"]}
        write_roles(chains)
        for role, refs in chains.items():
            self.cfg.roles[role] = list(refs)
        from alice.providers import ranking

        cache = ranking.load_cache()
        if cache:
            self.router.reload_auto(cache)
        return {"ok": True}

    # Google
    def _google_open(self, d: dict[str, Any]) -> dict[str, Any]:
        import webbrowser

        from alice.setup_google import STEPS

        i = int(d.get("step", 0))
        if not 0 <= i < len(STEPS):
            return {"ok": False, "error": "adım yok"}
        webbrowser.open(STEPS[i][1])
        return {"ok": True}

    def _google_client(self, d: dict[str, Any]) -> dict[str, Any]:
        """İstemciyi kaydet: indirilenlerdeki JSON, yapıştırılan JSON ya da Client ID + secret."""
        from alice.config import set_env_value
        from alice.setup_google import find_downloaded_json, read_client_json

        raw = str(d.get("text", "")).strip()
        if d.get("use_download"):
            found = find_downloaded_json()
            if found is None:
                return {"ok": False, "error": "İndirilenler klasöründe client_secret_*.json yok"}
            cid, secret = read_client_json(found.read_text(encoding="utf-8"))
        elif raw.startswith("{"):
            cid, secret = read_client_json(raw)
        elif raw.endswith(".apps.googleusercontent.com"):
            cid, secret = raw, str(d.get("secret", "")).strip()
            if not secret:
                return {"ok": False, "error": "Client secret'ı da yapıştır"}
        else:
            return {"ok": False, "error": "Client ID ….apps.googleusercontent.com ile biter; ya da JSON'un içeriğini yapıştır"}
        set_env_value("GOOGLE_CLIENT_ID", cid)
        set_env_value("GOOGLE_CLIENT_SECRET", secret)
        return {"ok": True}

    # Telefon
    def _phone_enable(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.config import set_config_value

        on = bool(d.get("on"))
        self.cfg.data.setdefault("phone", {})["enabled"] = on
        set_config_value("phone", "enabled", on)
        if self.system is not None:  # telefon köprüsü çekirdeğin içinde: yeniden başlayınca açılır/kapanır
            from alice.core.service import read_state

            if read_state().get("running") or on:
                return self.system.action("restart" if read_state().get("running") else "start", {})
        return {"ok": True}

    def _phone_new_token(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.app.phone import token

        token(new=True)
        if self.system is not None and self.cfg.data.get("phone", {}).get("enabled"):
            from alice.core.service import read_state

            if read_state().get("running"):
                return self.system.action("restart", {})
        return {"ok": True}

    # DCS
    def _dcs_install(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.sims import dcs

        dirs = dcs.saved_games_dirs()
        if not dirs:
            return {"ok": False, "error": "DCS Saved Games klasörü bulunamadı (DCS bir kez açılmış olmalı)"}
        done = dcs.install(dirs) if d.get("on", True) else dcs.uninstall(dirs)
        return {"ok": True, "count": len(done)}

    def _dcs_kneeboard(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.sims import dcs
        from alice.sims import kneeboard as kb

        dirs = dcs.saved_games_dirs()
        if not dirs:
            return {"ok": False, "error": "DCS Saved Games klasörü bulunamadı"}
        path = dcs.latest_mission(dirs)
        info = dcs.read_mission(path) if path else None
        written = kb.write(kb.render(info, kb.load_notes()), kb.kneeboard_dirs(dirs))
        return {"ok": True, "pages": len(written), "mission": info.name if info else ""}

    # Eklentiler
    def _plugin_new(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice import plugins

        name = str(d.get("name", "")).strip()
        if not plugins._NAME.match(name):
            return {"ok": False, "error": "ad küçük harf, rakam, - ya da _ olsun ve harfle başlasın"}
        folder = plugins.plugin_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name.replace('-', '_')}.py"
        if path.exists():
            return {"ok": False, "error": f"zaten var: {path}"}
        path.write_text(plugins.TEMPLATE.format(name=name.replace("-", "_")), encoding="utf-8")
        try:
            open_file(path)
        except OSError:
            pass
        return {"ok": True, "path": str(path)}

    def _plugin_folder(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice import plugins

        folder = plugins.plugin_dir()
        folder.mkdir(parents=True, exist_ok=True)
        open_file(folder)
        return {"ok": True}

    # Kısayol
    def _shortcut(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.app.window import create_shortcut

        import subprocess

        try:
            made = create_shortcut()
        except (RuntimeError, subprocess.CalledProcessError) as e:
            return {"ok": False, "error": str(e)}
        if not made:
            return {"ok": False, "error": "kısayol oluşturulamadı (sadece Windows)"}
        return {"ok": True, "paths": [str(p) for p in made]}
