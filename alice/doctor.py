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
    fix = "Ayarlar → Kurulum → Google (komut: alice setup google)" if "GOOGLE_CLIENT_ID" in why or \
        not os.environ.get("GOOGLE_CLIENT_ID") else "Ayarlar → Kurulum → Takvimi bağla (komut: alice connect google_calendar)"
    return Check(name, "off", why, fix)


def check_gmail(cfg, network: bool = True) -> Check:
    name = "Gmail"
    from alice.connect import store
    from alice.tools import gmail

    accs = gmail.accounts()
    if not accs:
        return Check(name, "off", "bağlı hesap yok", "Ayarlar → Kurulum → Gmail ekle (komut: alice setup google)")
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
        return Check(name, "warn", "; ".join(parts + bad), "bozuk hesabı tekrar bağla: Ayarlar → Kurulum → Gmail ekle (aynı ad)")
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


def check_ocr(cfg) -> Check:
    name = "Ekran okuma (OCR)"
    from alice.reflex import perceive

    ok, why = perceive.ocr_available()
    if ok:
        return Check(name, "ok", "Windows OCR hazır: refleks ekrandaki yazıları da okur")
    return Check(name, "off", f"{why}; refleks sadece erişilebilirlik ağacıyla çalışır",
                 "pip install -e .[computer]" if "paket" in why else "")


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
        _safe("Ekran okuma (OCR)", lambda: check_ocr(cfg)),
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


PROBE_TIMEOUT = 30.0  # sn: "Hepsini dene"de bir sağlayıcı en fazla bu kadar beklenir (normalde 180 sn)
PROBE_TOKENS = 512  # düşünen modeller (GLM Flash) önce düşünür; 50 token'da cevaba gelmeden kesiliyordu


# Krediyle çalışan sağlayıcıda (Vercel) test için en ucuz görünen model: kalıp sırası önemli.
CHEAP_HINTS = ("flash-lite", "nano", "haiku", "mini", "small", "flash", "8b")


def test_refs(router, name: str, pc: Any, explicit: str = "", limit: int = 3) -> list[str]:
    """Bir sağlayıcıyı denemek için aday modeller (sırayla, en fazla limit): rolde yazılı olan, son ölçümde
    çalışanlar, ölçülmüş diğerleri, yoksa sağlayıcının canlı kataloğundan. Hiç ölçülmemiş sağlayıcı (SambaNova)
    önceden "atlandı" görünüyordu; bir model yoğunsa (Z.ai) kardeşi denenmeden kırmızı çıkıyordu."""
    from alice.providers import ranking

    out: list[str] = [explicit] if explicit else []
    try:
        results = (ranking.load_cache() or {}).get("results") or {}
    except Exception:  # noqa: BLE001
        results = {}
    mine = [r for r in results.values() if str(r.get("ref", "")).startswith(name + ":")]
    for r in sorted(mine, key=lambda r: (not r.get("ok"), -float(r.get("quality") or 0))):
        if r.get("error_kind") not in ("not_found", "auth"):
            out.append(str(r["ref"]))
    out += [f"{name}:{m}" for m in (getattr(pc, "models", None) or [])]
    if len(set(out)) < limit:
        try:
            ids = [str(m.get("id") or m.get("name") or "").removeprefix("models/")
                   for m in router.client(name).list_models()]
        except Exception:  # noqa: BLE001 - katalog alınamazsa elimizdekiyle dene
            ids = []
        ids = [i for i in ids if i and not any(x in i.lower() for x in ranking.EXCLUDE)]
        if not getattr(pc, "auto", True):  # krediyle: en ucuz görüneni
            def cheap(i: str) -> int:
                return next((n for n, h in enumerate(CHEAP_HINTS) if h in i.lower()), len(CHEAP_HINTS))
            ids.sort(key=cheap)
        else:
            ids.sort(key=lambda i: -(ranking.quality(f"{name}:{i}") or ranking.UNKNOWN_QUALITY))
        out += [f"{name}:{i}" for i in ids]
    seen: list[str] = []
    for ref in out:
        if ref not in seen:
            seen.append(ref)
    return seen[:limit]


def short_err(err: str, limit: int = 160) -> str:
    """Sağlayıcı hatasından okunur kısa metin: ham JSON yerine içindeki "message", bilinen durumlar Türkçe."""
    import re

    kind = ""
    m = re.match(r"\[(\w+)\]\s*", err)
    if m:
        kind, err = m.group(1), err[m.end():]
    low = err.lower()
    if "payment_method_required" in low or "payment method is required" in low:
        return "ödeme yöntemi istiyor: sağlayıcı ücretsiz kullanım için de hesaba kart eklenmesini şart koşuyor"
    if "insufficient" in low and ("credit" in low or "balance" in low or "funds" in low):
        return "kredisi bitmiş ya da yok"
    msg = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', err)
    text = (msg.group(1) if msg else err).replace("\\n", " ").strip()
    text = " ".join(text.split())
    prefix = {"auth": "anahtar reddedildi", "quota": "kota dolu ya da ödeme gerekli", "not_found": "model yok",
              "rate_limit": "hız sınırı/yoğun", "server": "sağlayıcı hatası", "too_large": "istek çok büyük"}.get(kind, "")
    out = f"{prefix}: {text}" if prefix and text else (prefix or text)
    return out if len(out) <= limit else out[:limit - 1] + "…"


def probe_providers(cfg: Any, emit: Callable[[str], None] = lambda s: None,
                    timeout: float = PROBE_TIMEOUT, factory: Callable[[Any], Any] | None = None) -> list[dict[str, Any]]:
    """Her sağlayıcıya kısa bir istek: `alice doctor` ve uygulamadaki Kurulum → Hepsini dene ortak kullanır.
    status: ok | warn (geçici: yoğun/hız sınırı) | fail | off (anahtar yok, kurulu değil, elle seçilen)."""
    from alice.providers.chatgpt import make_client
    from alice.providers.router import AllProvidersFailed, Router

    router = Router(cfg, max_wait=0, provider_factory=factory or (lambda pc: make_client(pc, timeout=timeout)))
    explicit: dict[str, str] = {}
    for chain in cfg.roles.values():
        for ref in chain:
            p = ref.split(":", 1)[0]
            if p in router.providers_cfg:  # "auto" bir sağlayıcı değil
                explicit.setdefault(p, ref)
    rows = []
    for name, pc in router.providers_cfg.items():
        key = "gerekmez" if not pc.needs_key else ("var" if pc.api_key else "yok")
        row = {"name": name, "key": key, "env": pc.api_key_env or "", "model": "", "status": "off", "detail": ""}
        rows.append(row)
        if not pc.enabled:
            row["detail"] = (f"anahtar yok ({pc.api_key_env})" if key == "yok"
                             else "hesap kimliği eksik" if "${" in pc.base_url else "kapalı")
            continue
        credit = not getattr(pc, "auto", True)
        refs = test_refs(router, name, pc, explicit.get(name, ""), limit=1 if credit else 3)
        if not refs:
            row["detail"] = "anahtar var ama model listesi alınamadı (adres ya da anahtar yanlış olabilir)"
            continue
        tried: list[str] = []
        for ref in refs:
            emit(f"{name} deneniyor ({ref.split(':', 1)[1]})…")
            row["model"] = ref
            t0 = time.monotonic()
            try:
                res = router.chat(ref, [{"role": "user", "content": "Reply with just: ok"}], max_tokens=PROBE_TOKENS)
            except AllProvidersFailed as e:
                err = (e.errors[-1] if e.errors else "hata").split(": ", 1)[-1]
                tried.append(f"{ref.split(':', 1)[1]}: {short_err(err, 70)}")
                if not pc.needs_key and ("unreachable" in err or "bağlanılamadı" in err):
                    row.update(status="off", detail="çalışmıyor (yerel model; kurmadıysan sorun değil)")
                    break
                if err.startswith("[auth]"):
                    row.update(status="fail", detail=short_err(err) + " · Ayarlar → Anahtarlar'dan kontrol et")
                    break
                if "payment" in err.lower() and "required" in err.lower():
                    row.update(status="fail", detail=short_err(err))
                    break  # diğer modeller de aynı hesapta: denemeye gerek yok
                if err.startswith(("[rate_limit]", "[server]")):
                    row.update(status="warn", detail="şu an yoğun ya da hız sınırında (sağlayıcı tarafı, geçici); "
                                                     "ALICE sıradaki modelle devam eder")
                elif "zaman aşımı" in err:
                    row.update(status="fail", detail=f"{timeout:.0f} sn'de cevap vermedi; şu an yavaş ya da takılı, "
                                                     "ALICE bir süre sıradaki modelle devam eder")
                else:
                    row.update(status="fail", detail=short_err(err))
                continue
            if not (res.text or "").strip():
                tried.append(f"{ref.split(':', 1)[1]}: boş cevap")
                row.update(status="fail", detail="boş cevap döndü")
                continue
            row.update(status="ok", detail=f"{time.monotonic() - t0:.1f} sn" + (" · kredi harcar (çok az)" if credit
                                                                                   else ""))
            break
        if row["status"] != "ok" and len(tried) > 1:
            row["detail"] += " · denenenler: " + "; ".join(tried)
        elif row["status"] == "ok" and tried:
            row["detail"] += f" · ilk denenen olmadı ({tried[0][:60]})"
    return rows
