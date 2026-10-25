"""Ayarlar → Otomasyon: kurallar, zamanlanmış görevler ve akışlar (`alice rules`, `alice tasks`, `alice feeds`).

Kural ve görev komut satırındaki gibi düz cümleyle yazılır ("GPU 85 dereceyi 2 dakika geçerse haber ver",
"her sabah 9'da Discord'u özetle"). Hızlı model cümleyi çevirir, uygulama ne anladığını gösterir, kullanıcı onaylayınca
kaydedilir. Çalışan çekirdek dosyaları her turda yeniden okur; yeniden başlatmak gerekmez.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from alice.core.rules import Rule, RuleSet, fmt_duration, parse_rule
from alice.core.schedule import Cron, ScheduledTask, TaskStore, parse_task


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else ""


def rule_view(r: Rule) -> dict[str, Any]:
    cond = f"{r.metric} {r.op} {r.threshold:g}" + (f", {fmt_duration(r.duration)} boyunca" if r.duration else "")
    # "Ekran kartı {value:.0f}°C" -> "Ekran kartı …°C": yer tutucular ekranda ham görünmesin
    message = re.sub(r"\{(value|match|key|threshold)(:[^}]*)?\}", "…", r.message)
    return {"id": r.id, "text": r.description or cond, "condition": cond, "message": message, "severity": r.severity,
            "modes": r.modes, "confirm": r.confirm, "agent": r.prompt if r.action == "agent" else "",
            "enabled": r.enabled}


def task_view(t: ScheduledTask) -> dict[str, Any]:
    try:
        nxt = _when(t.next_run(datetime.now().timestamp()).timestamp()) if t.enabled else ""
    except ValueError:
        nxt = "hiç gelmiyor"
    return {"id": t.id, "text": t.description or t.prompt[:80], "prompt": t.prompt, "when": Cron(t.cron).describe(),
            "cron": t.cron, "next": nxt, "last": _when(t.last_run), "result": t.last_result[:600],
            "catch_up": t.catch_up, "enabled": t.enabled, "pending": t.pending}


def open_file(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Automations:
    def __init__(self, router: Any, cfg: Any = None, rules_path: Path | None = None, tasks_path: Path | None = None):
        self.router, self.cfg = router, cfg
        self.rules_path, self.tasks_path = rules_path, tasks_path

    def view(self) -> dict[str, Any]:
        from alice.core.feeds import feeds_path, load_feeds
        from alice.core.service import read_state

        feeds, errors = load_feeds()
        return {"rules": [rule_view(r) for r in RuleSet(self.rules_path).rules],
                "tasks": [task_view(t) for t in TaskStore(self.tasks_path).tasks],
                "feeds": [{"name": f.name, "describe": f.describe(), "enabled": f.enabled, "agent": f.agent}
                          for f in feeds],
                "feed_errors": errors, "feeds_path": str(feeds_path()), "feeds_file": feeds_path().exists(),
                "core_running": bool(read_state().get("running"))}

    def action(self, action: str, d: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self, "_" + action, None) if action.isidentifier() else None
        if fn is None:
            return {"ok": False, "error": f"bilinmeyen işlem: {action}"}
        try:
            return fn(d)
        except (ValueError, TypeError, KeyError) as e:
            return {"ok": False, "error": str(e) or type(e).__name__}

    # ------------------------------------------------------------------ kurallar
    def _rule_parse(self, d: dict[str, Any]) -> dict[str, Any]:
        """Cümleyi kurala çevir ama kaydetme: kullanıcı ne anlaşıldığını görüp onaylasın."""
        from alice.core.store import Store
        from alice.providers.router import AllProvidersFailed

        text = str(d.get("text", "")).strip()
        if not text:
            return {"ok": False, "error": "boş"}
        try:
            with Store() as st:
                rule = parse_rule(text, self.router, st)
        except AllProvidersFailed as e:
            return {"ok": False, "error": f"model cevap vermedi: {e}"}
        return {"ok": True, "rule": rule.__dict__, "view": rule_view(rule)}

    def _rule_add(self, d: dict[str, Any]) -> dict[str, Any]:
        rule = Rule.from_dict(dict(d.get("rule") or {}))
        RuleSet(self.rules_path).add(rule)
        return {"ok": True}

    def _rule_rm(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"ok": RuleSet(self.rules_path).remove(str(d.get("id", "")))}

    def _rule_enable(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"ok": RuleSet(self.rules_path).set_enabled(str(d.get("id", "")), bool(d.get("on")))}

    # ------------------------------------------------------------------ görevler
    def _task_parse(self, d: dict[str, Any]) -> dict[str, Any]:
        from alice.providers.router import AllProvidersFailed

        text = str(d.get("text", "")).strip()
        if not text:
            return {"ok": False, "error": "boş"}
        try:
            task = parse_task(text, self.router)
        except AllProvidersFailed as e:
            return {"ok": False, "error": f"model cevap vermedi: {e}"}
        v = task_view(task)
        v["next"] = _when(task.next_run(datetime.now().timestamp()).timestamp())
        return {"ok": True, "task": task.__dict__, "view": v}

    def _task_add(self, d: dict[str, Any]) -> dict[str, Any]:
        task = ScheduledTask.from_dict(dict(d.get("task") or {}))
        ts = TaskStore(self.tasks_path)
        ts.add(task)
        return {"ok": True}

    def _task_rm(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"ok": TaskStore(self.tasks_path).remove(str(d.get("id", "")))}

    def _task_set(self, d: dict[str, Any], **change: Any) -> dict[str, Any]:
        ts = TaskStore(self.tasks_path)
        t = ts.get(str(d.get("id", "")))
        if t is None:
            return {"ok": False, "error": "görev yok"}
        for k, v in change.items():
            setattr(t, k, v)
        ts.save()
        return {"ok": True}

    def _task_run(self, d: dict[str, Any]) -> dict[str, Any]:
        """Bir sonraki çekirdek turunda çalışır (çekirdek kapalıysa açılınca)."""
        return self._task_set(d, pending=True)

    def _task_enable(self, d: dict[str, Any]) -> dict[str, Any]:
        return self._task_set(d, enabled=bool(d.get("on")))

    # ------------------------------------------------------------------ akışlar
    def _feed_open(self, d: dict[str, Any]) -> dict[str, Any]:
        """feeds.toml'u düzenleyicide aç; yoksa örnek dosyayla oluştur."""
        from alice.core.cli import FEED_TEMPLATE
        from alice.core.feeds import feeds_path

        p = feeds_path()
        created = not p.exists()
        if created:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(FEED_TEMPLATE, encoding="utf-8")
        try:
            open_file(p)
        except OSError as e:
            return {"ok": False, "error": f"açılamadı: {e} ({p})"}
        return {"ok": True, "created": created, "path": str(p)}

    def _feed_test(self, d: dict[str, Any]) -> dict[str, Any]:
        """Şimdi yokla, her öğe için koşulun ne dediğini göster (bildirim gitmez)."""
        from alice.core.feeds import FeedMonitor, load_feeds

        feeds, _ = load_feeds()
        feed = next((f for f in feeds if f.name == d.get("name")), None)
        if feed is None:
            return {"ok": False, "error": "akış yok"}
        decider = None
        if feed.when.ask or feed.escalate.ask:
            from alice.decider import Decider

            decider = Decider.from_config(self.cfg, self.router)
        try:
            verdicts = FeedMonitor(cfg=self.cfg, decider=decider).explain(feed)
        except Exception as e:  # noqa: BLE001 - servis hatası (yanlış anahtar, kanal yok): ekranda göster
            return {"ok": False, "error": str(e)[:400]}
        return {"ok": True, "total": len(verdicts), "passed": sum(v.ok for v in verdicts),
                "items": [{"title": v.title[:160], "body": v.body[:200], "ok": v.ok, "reason": v.reason,
                           "severity": v.severity} for v in verdicts[:20]]}
