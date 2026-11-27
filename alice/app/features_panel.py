"""First-run wizard and Settings → Features, behind /api/features.

The installer marks a fresh install with `setup.json` {"wizard": "pending"}; the app then opens the wizard: language,
connect a model (paste a free key and test it, or sign in with ChatGPT), pick optional features, install them.
"""

from __future__ import annotations

import os
from typing import Any

from alice import features
from alice.app.setup_panel import Job

# Free or cheap keys offered in the wizard, in this order. Labels and links come from the .env catalog.
QUICK_KEYS = ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY")


class FeaturesPanel:
    def __init__(self, state: Any):
        self.state = state
        self.cfg = state.cfg
        self.installer = features.Installer(after=self._turn_on, turn_off=self._turn_off)
        self.key_test = Job()

    # ------------------------------------------------------------------ view
    def lang(self) -> str:
        lang = str(features.wizard_state().get("lang") or self.cfg.data.get("ui", {}).get("language") or "en")
        return lang if lang in ("en", "tr") else "en"

    def _keys(self) -> list[dict[str, Any]]:
        from alice.config import env_catalog

        cat = {e["key"]: e for e in env_catalog()}
        by_env = {pc.api_key_env: (name, pc) for name, pc in self.state.router.providers_cfg.items() if pc.api_key_env}
        out = []
        for key in QUICK_KEYS:
            if key not in cat or key not in by_env:
                continue
            name, pc = by_env[key]
            out.append({"key": key, "provider": name, "label": cat[key].get("label") or name,
                        "url": cat[key].get("url") or "", "set": bool(os.environ.get(key, "").strip() or pc.api_key)})
        return out

    def has_model(self) -> bool:
        from alice.providers import chatgpt

        cfgs = self.state.router.providers_cfg.values()
        if any(pc.enabled and pc.needs_key and pc.api_key and not str(getattr(pc, "name", "")).startswith("cliproxy") for pc in cfgs):
            return True
        try:
            if chatgpt.logged_in():
                return True
        except Exception:  # noqa: BLE001 - not signed in / module missing
            pass
        try:  # a subscription signed in through the Claude Code proxy (#459)
            from alice.app import cliproxy

            return bool(cliproxy.settings().get("installed") and any(cliproxy.groups_ready().values()))
        except Exception:  # noqa: BLE001 - proxy not installed / not running
            return False

    def view(self) -> dict[str, Any]:
        w = features.wizard_state()
        lang = self.lang()
        return {"wizard": {"pending": w.get("wizard") == "pending", "lang": lang},
                "features": features.catalog(lang, self.cfg), "install": self.installer.view(), "has_model": self.has_model(),
                "keys": self._keys(), "key_test": self.key_test.view(), "windows": os.name == "nt"}

    # ------------------------------------------------------------------ actions
    def action(self, action: str, d: dict[str, Any]) -> dict[str, Any]:
        if action == "lang":
            lang = str(d.get("lang", ""))
            if lang not in ("en", "tr"):
                return {"ok": False, "error": f"unknown language: {lang}"}
            from alice.config import set_config_value

            features.set_wizard_state(lang=lang)
            self.cfg.data.setdefault("ui", {})["language"] = lang
            set_config_value("ui", "language", lang)
            from alice.i18n import set_language

            set_language(lang)  # texts built in this process (CLI, notifications) follow at once
            return {"ok": True}
        if action == "key":
            key, value = str(d.get("key", "")), str(d.get("value", "")).strip()
            if key not in QUICK_KEYS or not value:
                return {"ok": False, "error": "unknown key or empty value"}
            r = self.state.set_key(key, value)
            if not r.get("ok"):
                return r
            return self._test(key)
        if action == "key_test":
            return self._test(str(d.get("key", "")))
        if action in ("install", "apply"):
            ids = [str(i) for i in (d.get("ids") or []) if str(i) in features.BY_ID]
            remove = [str(i) for i in (d.get("remove") or []) if str(i) in features.BY_ID]
            return self.installer.start(ids, {"autostart": d.get("autostart", True) is not False}, remove)
        if action == "open":
            features.set_wizard_state(wizard="pending")
            return {"ok": True}
        if action == "done":
            import time

            features.set_wizard_state(wizard="done", finished=time.time())
            return {"ok": True}
        return {"ok": False, "error": f"unknown action: {action}"}

    def _test(self, key: str) -> dict[str, Any]:
        """A one-line request through the provider that owns `key`, in the background (the app polls key_test)."""
        provider = next((k["provider"] for k in self._keys() if k["key"] == key), "")
        if not provider:
            return {"ok": False, "error": f"no provider uses {key}"}

        def run(job: Job) -> dict[str, Any]:
            from alice.doctor import probe_providers

            rows = probe_providers(self.cfg, emit=job.say, only={provider})
            row = rows[0] if rows else {"status": "fail", "detail": "provider not found"}
            return {"key": key, "provider": provider, "status": row.get("status"), "detail": row.get("detail", ""),
                    "model": row.get("model", "")}

        return self.key_test.start(run)

    # ------------------------------------------------------------------ after install
    def _turn_on(self, ids: list[str], opts: dict[str, Any]) -> list[str]:
        """Switch on what was just installed. Voice and the phone live in the background core, so start it last."""
        from alice.config import set_config_value

        lines: list[str] = []
        for section, fid in (("voice", "voice"), ("phone", "phone")):
            if fid in ids:
                self.cfg.data.setdefault(section, {})["enabled"] = True
                set_config_value(section, "enabled", True)
                lines.append(f"{fid}: on")
        if "core" in ids:
            system = self.state.system
            if opts.get("autostart", True):
                r = system.action("autostart", {"on": True})
                lines.append("core: starts when you sign in" if r.get("ok") else f"autostart: {r.get('error')}")
            from alice.core.service import read_state

            r = system.action("restart" if read_state().get("running") else "start", {})
            lines.append("core: starting" if r.get("ok") else f"core: {r.get('error')}")
        return lines

    def _turn_off(self, ids: list[str]) -> list[str]:
        """Before removing: switch voice and the phone off, take the core out of autostart and stop it."""
        from alice.config import set_config_value
        from alice.core.service import read_state

        lines: list[str] = []
        for section in ("voice", "phone"):
            if section in ids:
                self.cfg.data.setdefault(section, {})["enabled"] = False
                set_config_value(section, "enabled", False)
                lines.append(f"{section}: off")
        system = self.state.system
        if "core" in ids:
            system.action("autostart", {"on": False})
            lines.append("core: no longer starts at sign-in")
            if read_state().get("running"):
                r = system.action("stop", {})
                lines.append("core: stopping" if r.get("ok") else f"core: {r.get('error')}")
        elif {"voice", "phone"} & set(ids) and read_state().get("running"):
            r = system.action("restart", {})  # voice and the phone live in the core
            lines.append("core: restarting" if r.get("ok") else f"core: {r.get('error')}")
        return lines
