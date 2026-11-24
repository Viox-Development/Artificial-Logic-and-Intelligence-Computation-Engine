"""System → Setup: remaining command-line tasks in one place.

* Probe providers (`alice doctor`): a short request to each provider, decision engine, features status.
* Re-benchmark models (`alice models --refresh`) and set roles to automatic (`--write`).
* Google setup (`alice setup google`): step-by-step pages, finding downloaded JSON and writing keys.
* Phone (`alice phone`): enable/disable, address and QR code, new key.
* DCS (`alice dcs`): install/remove export script, write kneeboard pages.
* Plugins (`alice plugins`): list, errors, new plugin template.
* Desktop shortcut (`alice shortcut`).
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from alice.app.automations import open_file
from alice.i18n import _


class Job:
    """A single background job: state, progress lines, and result. The app reads view() frequently."""

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
            return {"ok": False, "error": _("already running")}
        with self._lock:
            self.s = {"state": "running", "lines": [], "result": None, "error": "", "started": time.time()}

        def run() -> None:
            try:
                result = fn(self)
                with self._lock:
                    self.s.update(state="done", result=result)
            except Exception as e:  # noqa: BLE001 - show on screen
                with self._lock:
                    self.s.update(state="error", error=str(e) or type(e).__name__)

        self._thread = threading.Thread(target=run, daemon=True, name="alice-setup-job")
        self._thread.start()
        return {"ok": True}


def qr_svg(text: str) -> str:
    """SVG QR for the address to be scanned with a phone (empty if the qrcode package is missing)."""
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
        self.chatgpt = Job()

    # ------------------------------------------------------------------ view
    def view(self) -> dict[str, Any]:
        from alice import plugins
        from alice.providers import ranking
        from alice.providers import chatgpt
        from alice.setup_google import STEPS, find_downloaded_json

        cache = ranking.load_cache()
        found = find_downloaded_json()
        return {
            "doctor": self.doctor.view(),
            "models": {**self.models.view(), "probed_at": cache.get("probed_at", ""),
                       "chains": cache.get("chains") or {},
                       "auto": all("auto" in (self.cfg.roles.get(r) or []) for r in ("brain", "fast", "vision"))},
            "google": {"steps": [{"title": _(t), "url": u, "how": _(h)} for t, u, h in STEPS],
                       "client": bool(os.environ.get("GOOGLE_CLIENT_ID", "").strip()),
                       "found_json": found.name if found else ""},
            "chatgpt": {**self._chatgpt_status(chatgpt), **self.chatgpt.view()},
            "phone": self._phone(),
            "dcs": self._dcs(),
            "plugins": {"dir": str(plugins.plugin_dir()), "errors": plugins.errors(),
                        "items": [{"name": p.name, "version": p.version, "tools": sorted(p.tool_names()),
                                   "mode": "her zaman" if p.always else "gerekince", "source": p.source}
                                  for p in plugins.active()]},
            "windows": os.name == "nt",
        }

    def _phone(self) -> dict[str, Any]:
        from alice.app import phone_apk
        from alice.app.phone import lan_ip, pair_url, token

        p = self.cfg.data.get("phone", {})
        out: dict[str, Any] = {"enabled": bool(p.get("enabled")), "url": "", "qr": "", "app": phone_apk.view(),
                               "qr_lib": importlib.util.find_spec("qrcode") is not None}
        if out["enabled"]:
            phone_apk.refresh_async()
            # Phone app address: Android app recognizes this QR (…/m?t=…); if no app, the page
            # opens in the browser and offers to download and install the app from the computer.
            url = pair_url(lan_ip(), int(p.get('port', 8765)), token())
            out.update(url=url, qr=qr_svg(url))
        return out

    @staticmethod
    def _dcs() -> dict[str, Any]:
        from alice.sims import dcs

        dirs = dcs.saved_games_dirs()
        m = dcs.latest_mission(dirs) if dirs else None
        return {"dirs": [{"path": str(d), "installed": (d / "Scripts" / "alice_export.lua").exists()} for d in dirs],
                "mission": m.name if m else ""}

    # ------------------------------------------------------------------ actions
    ACTIONS = {"weights_set", "doctor_run", "models_refresh", "models_auto", "google_open", "google_client", "phone_enable",
               "phone_new_token", "dcs_install", "dcs_kneeboard", "plugin_new", "plugin_folder", "shortcut", "chatgpt_login",
               "chatgpt_logout", "chatgpt_use", "chatgpt_usage", "chatgpt_model_add", "chatgpt_models_retry"}

    def action(self, action: str, d: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self, "_" + action) if action in self.ACTIONS else None
        if fn is None:
            return {"ok": False, "error": _("unknown action: {action}", action=action)}
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
            decider_line, decider_ok = _("not working: {error}", error=e), False
        job.say(_("Jev image test…"))
        from alice.decider import image_check

        image = image_check(decider)
        job.say(_("checking the features…"))
        checks = [c.__dict__ for c in run_all(self.cfg, Path.home())]
        return {"providers": providers, "decider": decider_line, "decider_ok": decider_ok, "checks": checks,
                "image": image}

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

        weights = ranking.load_weights()
        # Do not keep the order from benchmark time (built with the old formula): rebuild on each launch with current
        # weights (models are not remeasured, takes milliseconds). Previously numbers did not match the scores.
        cache = ranking.rebuild(self.router, weights) if (ranking.load_cache().get("results")) else ranking.load_cache()
        # The order ALICE actually uses (the main model chain is rearranged by score during runtime).
        chains = {}
        for role in ranking.DEFAULT_WEIGHTS:
            try:
                chains[role] = list(self.router.auto_chain(role))
            except Exception:  # noqa: BLE001 - dummy router (tests) or old cache
                chains[role] = list((cache.get("chains") or {}).get(role) or [])
        cache = {**cache, "chains": chains}
        return {"metrics": {k: _(v) for k, v in ranking.METRICS.items()}, "weights": weights, "defaults": ranking.DEFAULT_WEIGHTS,
                "rows": ranking.score_table(cache, weights, self.router.providers_cfg), "chains": chains,
                "probed_at": cache.get("probed_at", ""), "state": self.models.view()["state"]}

    def _weights_set(self, d: dict[str, Any]) -> dict[str, Any]:
        """Save weights of a role (or revert to default) and rebuild chains from the latest benchmark."""
        from alice.providers import ranking

        weights = ranking.load_weights()
        role = str(d.get("role", ""))
        if role and role not in ranking.DEFAULT_WEIGHTS:  # also on reset: unknown role was a KeyError
            return {"ok": False, "error": f"bilinmeyen rol: {role}"}
        if d.get("reset"):
            weights = {r: dict(w) for r, w in ranking.DEFAULT_WEIGHTS.items()} if not role else \
                {**weights, role: dict(ranking.DEFAULT_WEIGHTS[role])}
        else:
            if not role:
                return {"ok": False, "error": "rol gerekli"}
            new = d.get("weights") or {}
            if not isinstance(new, dict):
                return {"ok": False, "error": _("the weights must map metric → number")}
            try:
                vals = {k: float(new.get(k, weights[role][k])) for k in ranking.METRICS}
            except (TypeError, ValueError):
                return {"ok": False, "error": _("the weights must be numbers")}
            if any(not 0 <= v <= 100 for v in vals.values()) or not sum(vals.values()):
                return {"ok": False, "error": _("the weights must be 0-100 and can't all be zero")}
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
    def _chatgpt_login(self, d: dict[str, Any]) -> dict[str, Any]:
        """ChatGPT login in the browser; once done, the client in router is refreshed to benchmark models."""
        from alice.providers import chatgpt

        def run(job: Job) -> dict[str, Any]:
            job.say(_("opening the ChatGPT sign-in in the browser; sign in and allow it (within 5 min)"))
            info = chatgpt.login(emit=job.say)
            self._chatgpt_reset()
            job.say(_("signed in: {account} ({plan})", account=info.get("email") or _("ChatGPT account"), plan=info.get("plan") or _("plan unknown")))
            models = self._chatgpt_models(job)
            try:
                chatgpt.fetch_usage()
            except Exception:  # noqa: BLE001 - limit is also learned in the first response
                pass
            return {"email": info.get("email", ""), "plan": info.get("plan", ""), "models": models}

        return self.chatgpt.start(run)

    def _chatgpt_logout(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.providers import chatgpt

        ok = chatgpt.remove(str(d.get("key") or "") or None)
        self._chatgpt_reset()
        return {"ok": ok}

    def _chatgpt_use(self, d: dict[str, Any]) -> dict[str, Any]:
        """Switch to another registered account; do not carry the previous account's rate limit cooldown over."""
        from alice.providers import chatgpt

        if not chatgpt.use(str(d.get("key") or "")):
            return {"ok": False, "error": "hesap yok"}
        self._chatgpt_reset()
        return {"ok": True}

    def _chatgpt_models(self, job: Job | None = None) -> list[str]:
        """Read models accessible by the account: show them in the model menu right away."""
        from alice.providers.chatgpt import ChatGPTProvider

        pc = (getattr(self.router, "providers_cfg", None) or {}).get("chatgpt")
        if pc is None:
            return []
        try:
            models = [m["id"] for m in ChatGPTProvider(pc).list_models()]
        except Exception as e:  # noqa: BLE001 - show on screen
            if job:
                job.say(_("could not get the model list: {error}", error=e))
            return []
        if job:
            job.say(_("{n} models available: {models}. You can pick them from the model menu; for them to join the automatic "
                      "order, use 'Measure again'.", n=len(models), models=", ".join(models[:6])))
        return models

    def _chatgpt_status(self, chatgpt: Any) -> dict[str, Any]:
        pc = (getattr(self.router, "providers_cfg", None) or {}).get("chatgpt")
        if pc is not None and chatgpt.logged_in():
            chatgpt.ensure_models(pc)
        return chatgpt.status()

    def _chatgpt_model_add(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.providers import chatgpt

        name = str(d.get("name", "")).strip()
        if not name or " " in name:
            return {"ok": False, "error": _("enter a model name (e.g. gpt-5.5)")}
        return {"ok": True, "models": chatgpt.add_model(name)}

    def _chatgpt_models_retry(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.providers import chatgpt

        pc = (getattr(self.router, "providers_cfg", None) or {}).get("chatgpt")
        if pc is None or not chatgpt.logged_in():
            return {"ok": False, "error": _("sign in first")}
        chatgpt.retry_models(pc)
        return {"ok": True}

    def _chatgpt_usage(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.providers import chatgpt

        try:
            limits = chatgpt.fetch_usage()
        except Exception as e:  # noqa: BLE001 - last known remains
            return {"ok": False, "error": _("could not read the limit: {error}", error=e)}
        if not chatgpt.cached_models():
            self._chatgpt_models()
        return {"ok": True, "limits": limits}

    def _chatgpt_reset(self) -> None:
        r = self.router
        for name in ("cooldown_until", "cooldown_kind"):
            table = getattr(r, name, None)
            if isinstance(table, dict):
                for ref in [k for k in table if str(k).startswith("chatgpt:")]:
                    del table[ref]
        dead = getattr(r, "dead_providers", None)
        if isinstance(dead, set):
            dead.discard("chatgpt")

    def _google_open(self, d: dict[str, Any]) -> dict[str, Any]:
        import webbrowser

        from alice.setup_google import STEPS

        i = int(d.get("step", 0))
        if not 0 <= i < len(STEPS):
            return {"ok": False, "error": _("no step")}
        webbrowser.open(STEPS[i][1])
        return {"ok": True}

    def _google_client(self, d: dict[str, Any]) -> dict[str, Any]:
        """Save client: JSON in downloads, pasted JSON, or Client ID + secret."""
        from alice.config import set_env_value
        from alice.setup_google import find_downloaded_json, read_client_json

        raw = str(d.get("text", "")).strip()
        if d.get("use_download"):
            found = find_downloaded_json()
            if found is None:
                return {"ok": False, "error": _("no client_secret_*.json in the Downloads folder")}
            cid, secret = read_client_json(found.read_text(encoding="utf-8"))
        elif raw.startswith("{"):
            cid, secret = read_client_json(raw)
        elif raw.endswith(".apps.googleusercontent.com"):
            cid, secret = raw, str(d.get("secret", "")).strip()
            if not secret:
                return {"ok": False, "error": _("Paste the Client secret too")}
        else:
            return {"ok": False, "error": _("A Client ID ends in ….apps.googleusercontent.com; or paste the content of the JSON")}
        set_env_value("GOOGLE_CLIENT_ID", cid)
        set_env_value("GOOGLE_CLIENT_SECRET", secret)
        return {"ok": True}

    # Phone
    def _phone_enable(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.config import set_config_value

        on = bool(d.get("on"))
        self.cfg.data.setdefault("phone", {})["enabled"] = on
        set_config_value("phone", "enabled", on)
        if self.system is not None:  # phone bridge is inside core: starts/stops when restarted
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
            return {"ok": False, "error": _("DCS Saved Games folder not found (DCS must have been opened once)")}
        done = dcs.install(dirs) if d.get("on", True) else dcs.uninstall(dirs)
        return {"ok": True, "count": len(done)}

    def _dcs_kneeboard(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.sims import dcs
        from alice.sims import kneeboard as kb

        dirs = dcs.saved_games_dirs()
        if not dirs:
            return {"ok": False, "error": _("DCS Saved Games folder not found")}
        path = dcs.latest_mission(dirs)
        info = dcs.read_mission(path) if path else None
        written = kb.write(kb.render(info, kb.load_notes()), kb.kneeboard_dirs(dirs))
        return {"ok": True, "pages": len(written), "mission": info.name if info else ""}

    # Plugins
    def _plugin_new(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice import plugins

        name = str(d.get("name", "")).strip()
        if not plugins._NAME.match(name):
            return {"ok": False, "error": _("the name must use lowercase letters, digits, - or _ and start with a letter")}
        folder = plugins.plugin_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name.replace('-', '_')}.py"
        if path.exists():
            return {"ok": False, "error": f"zaten var: {path}"}
        path.write_text(plugins.TEMPLATE.format(name=name.replace("-", "_")), encoding="utf-8")
        try:
            open_file(path)
        except OSError:
            pass  # the file is created; the user can open it themselves
        return {"ok": True, "path": str(path)}

    def _plugin_folder(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice import plugins

        folder = plugins.plugin_dir()
        folder.mkdir(parents=True, exist_ok=True)
        open_file(folder)
        return {"ok": True}

    # Shortcut
    def _shortcut(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.app.window import create_shortcut

        import subprocess

        try:
            made = create_shortcut()
        except (RuntimeError, subprocess.CalledProcessError) as e:
            return {"ok": False, "error": str(e)}
        if not made:
            return {"ok": False, "error": _("could not create the shortcut (Windows only)")}
        return {"ok": True, "paths": [str(p) for p in made]}
