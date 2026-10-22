"""`alice doctor` için yeni özelliklerin kontrolü: her biri (ad, durum, ayrıntı) satırı döndürür.

Durum: "ok" çalışıyor | "warn" çalışıyor ama eksik/kısıtlı | "off" kapalı/kurulmamış (hata değil) | "fail" bozuk.
Ağ isteği atan kontroller kısa zaman aşımıyla çalışır; biri düşse de diğerleri sürer.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Check:
    name: str
    status: str  # ok | warn | off | fail
    detail: str
    fix: str = ""


def _safe(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - bir kontrol çökerse onu "fail" yaz, diğerleri sürsün
        return Check(name, "fail", f"{type(e).__name__}: {str(e)[:160]}")


# ------------------------------------------------------------------------------------------- kontroller
def check_grounding(cfg, network: bool = True) -> Check:
    name = "Google araması"
    if not cfg.agent.get("search_grounding", True):
        return Check(name, "off", "kapalı ([agent] search_grounding = false)")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return Check(name, "off", "GEMINI_API_KEY yok: web_search DuckDuckGo/Bing kullanır", "GEMINI_API_KEY ekle")
    if not network:
        return Check(name, "ok", "anahtar var (test edilmedi)")
    from alice.tools import web

    t0 = time.monotonic()
    try:
        res = web.gemini_search("bugünün tarihi", key, str(cfg.agent.get("grounding_model", "gemini-flash-latest")),
                                timeout=20)
    except ValueError as e:
        return Check(name, "warn", f"çalışmıyor ({str(e)[:120]}); DuckDuckGo/Bing'e düşülüyor",
                     "ücretsiz anahtarda grounding kapalı olabilir; sorun değil, klasik arama çalışır")
    finally:
        web._grounding["down_until"] = 0.0  # doctor'un denemesi gerçek kullanımı bir saat kilitlemesin
    return Check(name, "ok", f"çalışıyor ({time.monotonic() - t0:.1f} sn, {len(res['sources'])} kaynak)")


def check_embedding(cfg, network: bool = True) -> Check:
    name = "Anlam araması (hafıza)"
    from alice.memvec import Embedder

    o = dict(cfg.data.get("memory") or {})
    emb = Embedder.from_config(cfg, o)
    if emb is None:
        return Check(name, "off", f"embedding yok (sağlayıcı: {o.get('embed_provider', 'gemini')}, anahtar yok ya da off)"
                     "; hafıza kelime aramasıyla çalışır")
    if not network:
        return Check(name, "ok", f"{emb.model} (test edilmedi)")
    t0 = time.monotonic()
    try:
        vec = emb.embed(["deneme"])[0]
    except Exception as e:  # noqa: BLE001
        return Check(name, "warn", f"{emb.model} çalışmıyor: {str(e)[:120]}; kelime aramasına düşülüyor")
    return Check(name, "ok", f"{emb.model}, {len(vec)} boyut, {time.monotonic() - t0:.1f} sn")


def check_memory(cfg) -> Check:
    name = "Hafıza"
    from alice.config import alice_home

    if not (cfg.data.get("memory") or {}).get("enabled", True):
        return Check(name, "off", "kapalı ([memory] enabled = false)")
    if not (alice_home() / "memory.db").is_file():
        return Check(name, "ok", "henüz boş (ilk konuşmalarla dolacak)")
    from alice.memory import shared_memory

    mem = shared_memory(cfg)
    if mem is None:
        return Check(name, "off", "kapalı")
    with mem._lock:
        vecs = mem.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    sums = len(mem.summaries(1000))
    return Check(name, "ok", f"{len(mem.facts())} bilgi, {sums} konuşma özeti, {len(mem.followups())} açık takip, "
                 f"{vecs} vektör")


def check_projects(cfg) -> Check:
    name = "Projeler"
    from alice import projects

    if not projects.enabled(cfg):
        return Check(name, "off", "kapalı ([projects] enabled = false)")
    scanned, found = projects.load()
    if not scanned:
        return Check(name, "warn", "henüz taranmadı (ilk açılışta arka planda taranır)", "alice ile bir konuşma başlat")
    age = (time.time() - scanned) / 3600
    top = ", ".join(p.name for p in found[:4])
    return Check(name, "ok" if found else "warn", f"{len(found)} proje ({age:.0f} saat önce tarandı)"
                 + (f": {top}" if top else ""), "" if found else "[projects] paths'e proje klasörünü ekle")


def check_calendar(cfg) -> Check:
    name = "Google Takvim"
    from alice.connect import runner
    from alice.connect.manifest import discover

    m = discover(cfg)[0].get("google_calendar")
    if m is None:
        return Check(name, "off", "bağlantı tanımı kapalı")
    ok, why = runner.ready(m)
    if ok:
        return Check(name, "ok", "bağlı")
    fix = "alice setup google" if "GOOGLE_CLIENT_ID" in why or not os.environ.get("GOOGLE_CLIENT_ID") else \
        "alice connect google_calendar"
    return Check(name, "off", why, fix)


def check_gmail(cfg, network: bool = True) -> Check:
    name = "Gmail"
    from alice.connect import store
    from alice.tools import gmail

    accs = gmail.accounts()
    if not accs:
        return Check(name, "off", "bağlı hesap yok", "alice setup google (ya da alice connect gmail --account <ad>)")
    parts, bad = [], []
    for acc in accs:
        email = store.get(gmail.PREFIX + acc).get("email", "")
        if network:
            try:
                gmail.token(acc)
            except Exception as e:  # noqa: BLE001
                bad.append(f"{acc}: {str(e)[:80]}")
                continue
        parts.append(f"{acc}" + (f" ({email})" if email else ""))
    if bad:
        return Check(name, "warn", "; ".join(parts + bad), "bozuk hesabı tekrar bağla: alice connect gmail --account <ad>")
    return Check(name, "ok", ", ".join(parts))


def check_sandbox(cfg) -> Check:
    name = "Komut sandbox'ı"
    from alice import sandbox

    ctx = type("C", (), {"config": cfg})()
    m = sandbox.mode(ctx)
    if m == "off":
        return Check(name, "off", "kapalı ([agent] sandbox = off)")
    backend, problem = sandbox.backend()
    if problem:
        return Check(name, "warn", f"{m} modu ama kurulamıyor: {problem}"
                     + ("; dış içerik okunan turda komutlar onay sorar" if m == "auto" else "; komutlar hata verir"),
                     problem.split(": ", 1)[-1] if "apt" in problem else "")
    return Check(name, "ok", f"{m} modu, altyapı: {backend}")


def check_greeting(cfg) -> Check:
    name = "Dönüşte karşılama"
    from alice import presence

    s = presence.settings(cfg)
    if not s.get("enabled", True):
        return Check(name, "off", "kapalı ([greeting] enabled = false)")
    idle = presence.idle_seconds()
    if idle is None:
        return Check(name, "warn", "boşta kalma süresi ölçülemiyor (Linux'ta xprintidle gerekir)",
                     "sudo apt install xprintidle")
    return Check(name, "ok", f"{s['away_minutes']} dk yokluktan sonra; şu an {idle:.0f} sn boşta (çekirdek çalışırken)")


def check_rules(cfg, cwd=None) -> Check:
    name = "Kurallar, hook, skill"
    from alice.hooks import Hooks
    from alice.permissions import Permissions
    from alice.skills import load_agents, load_skills

    ctx = type("C", (), {"config": cfg, "cwd": cwd})()
    rules = Permissions("auto").rules_for(ctx)
    hooks = Hooks.from_config(cfg, cwd)
    return Check(name, "ok", f"izin kuralı {sum(len(v) for v in rules.values())} (allow {len(rules['allow'])}, deny "
                 f"{len(rules['deny'])}), hook {len(hooks.hooks)}, skill {len(load_skills(cwd))}, "
                 f"alt ajan {len(load_agents(cwd))}")


def check_update_health(cfg) -> Check:
    name = "Güncelleme sağlığı"
    from alice import updater

    st = updater.read_state()
    rb = st.get("rollback") if isinstance(st.get("rollback"), dict) else None
    bad = st.get("bad") or []
    if rb and time.time() - float(rb.get("ts") or 0) < 7 * 86400:
        return Check(name, "warn", f"son güncelleme geri alındı ({str(rb.get('from'))[:7]} → {str(rb.get('to'))[:7]}): "
                     f"{rb.get('reason')}", "düzeltme commit'i gelince otomatik güncellenir")
    if st.get("pending_health"):
        return Check(name, "ok", "yeni sürüm deneniyor (90 sn ayakta kalınca sağlam sayılır)")
    return Check(name, "ok", "sorun yok" + (f" ({len(bad)} bozuk sürüm atlandı)" if bad else ""))


def run_all(cfg, cwd=None, network: bool = True) -> list[Check]:
    return [
        _safe("Google araması", lambda: check_grounding(cfg, network)),
        _safe("Anlam araması (hafıza)", lambda: check_embedding(cfg, network)),
        _safe("Hafıza", lambda: check_memory(cfg)),
        _safe("Projeler", lambda: check_projects(cfg)),
        _safe("Google Takvim", lambda: check_calendar(cfg)),
        _safe("Gmail", lambda: check_gmail(cfg, network)),
        _safe("Komut sandbox'ı", lambda: check_sandbox(cfg)),
        _safe("Dönüşte karşılama", lambda: check_greeting(cfg)),
        _safe("Kurallar, hook, skill", lambda: check_rules(cfg, cwd)),
        _safe("Güncelleme sağlığı", lambda: check_update_health(cfg)),
    ]


def test_ref(router, name: str, pc: Any, explicit: str = "") -> str:
    """Bir sağlayıcıyı test etmek için model: rolde yazılı olan, yoksa ölçüm önbelleğinde o sağlayıcının çalışan en
    iyi modeli, yoksa sabit listesinin ilki. Roller "auto" iken sağlayıcılar "atlandı" görünüyordu."""
    if explicit:
        return explicit
    try:
        from alice.providers import ranking

        results = (ranking.load_cache() or {}).get("results") or {}
        best = sorted((r for r in results.values() if r.get("ok") and str(r.get("ref", "")).startswith(name + ":")),
                      key=lambda r: -float(r.get("quality") or 0))
        if best:
            return str(best[0]["ref"])
    except Exception:  # noqa: BLE001
        pass
    models = getattr(pc, "models", None) or []
    return f"{name}:{models[0]}" if models else ""
