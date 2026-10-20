"""Ücretsiz modelleri canlı ölçüp sıralama ("auto" rol zinciri).

Ücretsiz katalog sürekli değişiyor: modeller kalkıyor (404/410), "free" sürümü paralıya dönüyor,
bazıları sadece belirli araçlarda açılıyor. Sabit liste çürüyor. Burada:

1. Sağlayıcıların /models kataloglarından, bildiğimiz iyi ailelere uyan adaylar seçilir.
2. Her aday gerçek bir araç çağrısı isteğiyle denenir: geçti mi, toplam süre, ilk token süresi,
   üretilen token ve token/sn ölçülür.
3. Rol zincirleri kalite önce, hız sonra olacak şekilde kurulur ve ~/.alice/models.json'a yazılır.

config'te rol listesine "auto" yazılınca router bu sırayı kullanır. Önbellek bir günden eskiyse
uygulama açılışta arka planda yeniler.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

# Kalite puanı (0-100): model kimliğinde geçen ilk kalıp kazanır. Sıra önemli: özel kalıplar önce.
# Puanlar ajan işi (araç kullanma, Türkçe, kod) için; yeni aileler çıktıkça buraya eklenir.
QUALITY: list[tuple[str, int]] = [
    ("gemini-3.1-pro", 97), ("gemini-pro", 97), ("gemini-2.5-pro", 90),
    ("flash-lite", 66), ("gemini-3.8-flash", 93), ("gemini-flash-latest", 93), ("gemini-3.7-flash", 91),
    ("gemini-3.6-flash", 89), ("gemini-3.5-flash", 87), ("gemini-3-flash", 84), ("gemini-2.5-flash", 78),
    ("kimi-k3", 92), ("glm-5.3-flash", 74), ("glm-5", 90), ("glm-4.7-flash", 72), ("glm-4.5-flash", 64),
    ("glm-4.6v-flash", 60), ("command-a-plus", 80), ("command-a-vision", 62), ("command-a", 76), ("deepseek-v4", 88), ("deepseek-r", 80),
    ("nemotron-3-ultra", 87), ("qwen3.8", 85), ("qwen3-coder", 84), ("gpt-oss-120b", 82), ("kimi-k2", 82),
    ("mistral-large", 78), ("nemotron-3-super", 78), ("laguna-s", 76), ("dots-3", 74), ("llama-3.3-70b", 70),
    ("north-mini-code", 72), ("devstral", 72), ("mistral-medium", 72), ("gemma-4-31b", 68), ("ling-3", 66),
    ("gemma-4-26b", 64), ("nemotron-3.5-lightning", 62), ("gpt-oss-20b", 60), ("laguna-xs", 58),
    ("llama-4", 60), ("mistral-small", 58), ("qwen3:8b", 40),
]
# Sohbet/araç işi yapmayan ya da ajan için uygun olmayan modeller.
EXCLUDE = ("openrouter/free", "openrouter/auto", "tts", "image", "embed", "live", "audio", "guard", "safety",
           "reward", "parse", "translate", "transcribe", "robotics", "computer-use", "lyria", "veo", "banana",
           "customtools", "deep-research", "antigravity", "omni", "whisper", "orpheus", "aqa", "inkling",
           "content-safety", "vision-instruct", "kosmos", "fuyu", "deplot", "vila", "neva", "clip")
# Rastgele modele yönlendiren "router" modeller: sadece zincirin en sonunda, son çare.
RANDOM_ROUTERS = ("openrouter:openrouter/free", "openrouter:openrouter/auto")

CACHE_MAX_AGE = 24 * 3600
PROBE_TIMEOUT = 20.0  # 20 sn içinde araç çağrısı yapamayan model zaten günlük kullanıma uygun değil
PER_PROVIDER = 15
# OpenRouter'ın ücretsiz modelleri hesap başına günlük ortak bir sınırı paylaşıyor (kredisiz hesapta ~50 istek):
# günlük ölçüm bunun küçük bir kısmını harcasın.
PROVIDER_CAP = {"openrouter": 6}
# Kota hesap genelinde olan sağlayıcılar: bir modelde kota hatası gelince diğerleri denenmez (hepsi aynı kotada).
ACCOUNT_QUOTA = ("openrouter",)
SLOW = 3.0  # saniye: beyin rolünde bundan yavaş modeller geri itilir

# Önbellek yokken (ilk açılış, ölçüm henüz bitmemişken) kullanılan sıra: 9 Ekim 2026 ölçümü.
# Ölçüm gelince yerini alır; anahtarı olmayan sağlayıcıların modelleri zaten atlanır.
DEFAULT_AUTO: dict[str, list[str]] = {
    "brain": [
        "gemini:gemini-flash-latest", "gemini:gemini-3.8-flash", "nvidia:z-ai/glm-5.3",
        "gemini:gemini-3.6-flash", "gemini:gemini-3-flash-preview",
        "openrouter:nvidia/nemotron-3-super-120b-a12b:free", "nvidia:nvidia/nemotron-3-super-120b-a12b",
        "openrouter:dots-studio/dots-3-note-preview:free", "openrouter:cohere/north-mini-code:free",
    ],
    "fast": [
        "groq:qwen/qwen3.8-27b", "gemini:gemini-flash-lite-latest", "groq:openai/gpt-oss-20b",
        "nvidia:poolside/laguna-xs-2.1", "openrouter:nvidia/nemotron-3-super-120b-a12b:free",
    ],
    "vision": ["gemini:gemini-flash-latest", "gemini:gemini-3.8-flash", "gemini:gemini-3.6-flash"],
}


def quality(ref: str) -> int:
    low = ref.lower()
    if any(x in low for x in EXCLUDE):
        return 0
    for pat, score in QUALITY:
        if pat in low:
            return score
    return 0


@dataclass
class Probe:
    ref: str
    ok: bool
    quality: int
    latency: float = 0.0
    ttft: float = 0.0
    tokens: int = 0
    tps: float = 0.0
    tool: str = ""
    error_kind: str = ""
    error: str = ""
    ever_ok: bool = False  # önceki ölçümlerde en az bir kez çalıştı mı

    @property
    def dead(self) -> bool:
        """Kalıcı hata: model yok/kalkmış ya da bu anahtarla açılmıyor (rate limit geçicidir)."""
        return self.error_kind in ("not_found", "auth")


# ------------------------------------------------------------------ önbellek
def cache_path() -> Path:
    return alice_home() / "models.json"


def load_cache(path: Path | None = None) -> dict[str, Any]:
    try:
        return json.loads((path or cache_path()).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def cache_age(cache: dict[str, Any]) -> float:
    try:
        return time.time() - float(cache.get("probed_ts", 0))
    except (TypeError, ValueError):
        return float("inf")


def uses_auto(roles: dict[str, list[str]]) -> bool:
    return any("auto" in (chain or []) for chain in roles.values())


# ------------------------------------------------------------------- adaylar
def catalog_models(router, name: str) -> list[dict[str, Any]]:
    """Sağlayıcının model listesi: config'te models yazılıysa o (sadece ücretsizler), yoksa /models."""
    pc = router.providers_cfg[name]
    fixed = getattr(pc, "models", None) or []
    if fixed:
        return [{"id": m} for m in fixed]
    return router.client(name).list_models()


def candidates(router, per_provider: int = PER_PROVIDER) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
    """Kataloglardan bildiğimiz iyi ailelere uyan adaylar (sağlayıcı başına en iyi per_provider tane),
    katalog hataları ve her sağlayıcının tam model listesi (elle yazılmış ama kalkmış modelleri ayıklamak için)."""
    from alice.providers.discovery import _generic, _openrouter

    errors: dict[str, str] = {}
    catalog: dict[str, list[str]] = {}
    by_provider: dict[str, list[tuple[int, str]]] = {}
    for name, pc in router.providers_cfg.items():
        if name == "ollama":
            continue
        if not pc.enabled:
            errors[name] = f"{pc.api_key_env} tanımlı değil"
            continue
        if not getattr(pc, "auto", True):
            continue  # krediyle çalışan sağlayıcı: sadece elle seçilir, ölçümde bile kredi harcamasın
        try:
            models = catalog_models(router, name)
        except Exception as e:  # noqa: BLE001 - keşifte hiçbir hata ölümcül değil
            errors[name] = str(e)[:160]
            continue
        if not getattr(pc, "models", None):
            # Sabit liste gerçek katalog değil: elle yazılmış ücretli bir model (zai:glm-5) "kalkmış" sayılmasın.
            catalog[name] = sorted({str(m.get("id") or m.get("name") or "").removeprefix("models/")
                                    for m in models} - {""})
        for f in (_openrouter(models) if name == "openrouter" else _generic(name, models)):
            q = quality(f.ref)
            if q > 0 and f.tools is not False:
                by_provider.setdefault(name, []).append((q, f.ref))
    out = []
    for name, items in by_provider.items():
        items.sort(key=lambda x: -x[0])
        out += [ref for _, ref in items[:min(per_provider, PROVIDER_CAP.get(name, per_provider))]]
    return out, errors, catalog


# ---------------------------------------------------------------------- ölçüm
PROBE_TOOLS = ("list_dir", "run_command", "read_file", "process_list")
PROBE_PROMPT = "Masaüstümdeki (C:\\Users\\kullanici\\Desktop) dosyaları listele."


def _probe_specs() -> list[dict[str, Any]]:
    from alice.tools import all_tools

    return [t.spec() for t in all_tools() if t.name in PROBE_TOOLS]


def probe(router, ref: str, specs: list[dict[str, Any]] | None = None, timeout: float = PROBE_TIMEOUT) -> Probe:
    """Tek bir modeli gerçek bir araç çağrısı isteğiyle dene. Router'ın ceza mekanizmasına dokunmaz."""
    from alice.providers.openai_compat import OpenAICompatProvider, ProviderError
    from alice.providers.router import clean_messages, is_gemini, parse_ref

    p, model = parse_ref(ref)
    pc = router.providers_cfg[p]
    client = OpenAICompatProvider(pc, timeout=timeout)
    msgs = clean_messages([
        {"role": "system", "content": "You are ALICE, an agent on the user's Windows PC. Use tools to act."},
        {"role": "user", "content": PROBE_PROMPT},
    ], gemini=is_gemini(pc))
    res = Probe(ref, False, quality(ref))
    first: list[float] = []
    t0 = time.monotonic()
    try:
        r = client.chat_stream(model, msgs, lambda s: None, tools=specs or _probe_specs(),
                               extra=router.config.model_extra(ref) or None,
                               on_first=lambda: first.append(time.monotonic()))
    except ProviderError as e:
        res.latency = round(time.monotonic() - t0, 2)
        res.error_kind, res.error = e.kind, str(e)[:300]
        return res
    except Exception as e:  # noqa: BLE001 - ölçümde hiçbir hata ölümcül değil
        res.latency = round(time.monotonic() - t0, 2)
        res.error_kind, res.error = "network", f"{type(e).__name__}: {e}"[:300]
        return res
    total = time.monotonic() - t0
    calls = r.message.get("tool_calls") or []
    res.latency = round(total, 2)
    res.ttft = round((first[0] - t0) if first else total, 2)
    out_chars = len(r.text) + sum(len(c["function"].get("arguments", "")) for c in calls)
    res.tokens = int((r.usage or {}).get("completion_tokens") or out_chars / 3.5)
    gen = total - res.ttft
    res.tps = round(res.tokens / gen, 1) if gen > 0.05 else 0.0
    good = [c for c in calls if c["function"]["name"] in PROBE_TOOLS]
    if good:
        try:
            json.loads(good[0]["function"].get("arguments") or "{}")
            res.ok, res.tool = True, good[0]["function"]["name"]
        except json.JSONDecodeError:
            res.error_kind, res.error = "bad_args", "araç argümanları geçerli JSON değil"
    else:
        res.error_kind, res.error = "no_tool", f"araç çağırmadı: {r.text[:120]!r}"
    return res


def probe_all(router, refs: list[str], emit: Callable[[str], None] = lambda s: None,
              timeout: float = PROBE_TIMEOUT) -> list[Probe]:
    """Sağlayıcı başına ayrı işçi; sağlayıcı içinde birkaç eşzamanlı istek (Gemini/Groq'ta 1-2)."""
    specs = _probe_specs()
    groups: dict[str, list[str]] = {}
    for ref in refs:
        groups.setdefault(ref.split(":", 1)[0], []).append(ref)
    results: list[Probe] = []
    lock = threading.Lock()

    def run_group(provider: str, items: list[str]) -> None:
        workers = {"gemini": 2, "groq": 1, "cerebras": 2}.get(provider, 4)
        if provider in ACCOUNT_QUOTA:
            workers = 1  # sırayla: kota dolunca kalanlara istek gitmesin
        quota_hit: list[Probe] = []

        def one(ref: str) -> Probe:
            if quota_hit:
                return Probe(ref, False, quality(ref), error_kind="quota",
                             error="denenmedi: sağlayıcının günlük ücretsiz kotası dolu")
            pr = probe(router, ref, specs, timeout)
            if pr.error_kind == "quota" and provider in ACCOUNT_QUOTA:
                quota_hit.append(pr)
            return pr

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for pr in pool.map(one, items):
                with lock:
                    results.append(pr)
                state = f"OK {pr.latency:.1f}s" if pr.ok else f"{pr.error_kind}"
                emit(f"{pr.ref}: {state}")

    threads = [threading.Thread(target=run_group, args=(p, items), daemon=True) for p, items in groups.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# -------------------------------------------------------------------- sıralama
def brain_score(pr: Probe) -> float:
    return brain_score_of(pr.quality, pr.latency)


def brain_score_of(quality: float, latency: float) -> float:
    # Ajan bir işte 10-20 kez model çağırıyor: 3 sn üstündeki her saniye kaliteden 1,5 puan götürür.
    # (Önce 20 sn sonrası cezalıydı; ~8 sn'lik NVIDIA glm-5.3 zincirin başında kalıyor, basit iş 4 dk sürüyordu.)
    slow = max(0.0, latency - SLOW)
    return quality - min(30.0, slow * 1.5)


def fast_score(pr: Probe) -> float:
    # Hızlı rol: kalite yeterliyse süreye göre. 1 sn ~ +30, 10 sn ~ +3.
    return min(40.0, 30.0 / max(pr.latency, 0.25)) + pr.quality * 0.3


def voice_score(pr: Probe) -> float:
    # Sesli asistan: ilk kelimeye kadar geçen süre (TTFT) her saniye 15 puan götürür. Ölçümdeki TTFT kısa
    # istekle; gerçek sesli istekte (uzun sistem istemi) yavaş modeller 5-10 kat yavaşlıyor.
    return pr.quality - pr.ttft * 15


def _diversify(ranked: list[Probe], score: Callable[[Probe], float], gap: float = 12.0) -> list[Probe]:
    """Aynı sağlayıcıdan art arda ikiden fazla model koyma (puan farkı gap'ten azsa): bir sağlayıcı
    yoğunluktan 503 verince ya da kotası dolunca zincirin geri kalanı da onunla birlikte düşmesin."""
    left = list(ranked)
    out: list[Probe] = []
    while left:
        pick = left[0]
        prov = [r.ref.split(":", 1)[0] for r in out[-2:]]
        if len(prov) == 2 and prov[0] == prov[1] == pick.ref.split(":", 1)[0]:
            alt = next((r for r in left if r.ref.split(":", 1)[0] != prov[0]
                        and score(pick) - score(r) <= gap), None)
            pick = alt or pick
        out.append(pick)
        left.remove(pick)
    return out


def vision_backups(results: dict[str, Any], providers_cfg: dict[str, Any], have: list[str],
                   limit: int = 2) -> list[str]:
    """Görme zincirinin başı genelde hep Gemini: hepsi aynı günlük kotada tükeniyor, ekran görevi de "görme modeli
    yok" diye düşüyordu. Zincirde olmayan sağlayıcılardan ölçümde çalışmış en iyi görsel modeller (sağlayıcı başına
    bir) yedek olarak sona eklenir."""
    used = {r.split(":", 1)[0] for r in have}
    out: list[str] = []
    for ref, r in sorted(results.items(), key=lambda kv: -brain_score_of(kv[1].get("quality", 0),
                                                                          kv[1].get("latency", 0))):
        prov, _, model = ref.partition(":")
        pc = providers_cfg.get(prov)
        if (len(out) < limit and r.get("ok") and prov not in used and ref not in have and pc
                and pc.supports_vision(model) and quality(ref) > 0):
            used.add(prov)
            out.append(ref)
    return out


def build_chains(results: list[Probe], providers_cfg: dict[str, Any], limit: int = 10) -> dict[str, list[str]]:
    ok = [r for r in results if r.ok and r.quality > 0]

    def provider_cfg(ref: str):
        return providers_cfg.get(ref.split(":", 1)[0])

    # Beyin istekleri sistem istemi + araçlarla ~8-12K token: küçük TPM'li sağlayıcılar (Groq) beyne uymaz.
    def roomy(ref: str) -> bool:
        pc = provider_cfg(ref)
        return bool(pc and pc.max_input_tokens >= 16000)

    brain = [r for r in ok if r.quality >= 60 and roomy(r.ref)]
    brain = _diversify(sorted(brain, key=lambda r: -brain_score(r)), brain_score)
    # Ölçüm anında geçici hata veren (rate limit, 5xx) iyi modeller de sona yedek olarak girer.
    # Zaman aşımına uğrayanlar girmez: o kadar yavaş bir model beyin için işe yaramaz.
    # Günlük kotası dolmuş ama daha önce çalıştığı görülmüş modeller de (kota ertesi gün döner); hiç
    # çalışmamış kota hatalıları (ör. ücretsiz katmanda kotası 0 olan pro modeller) girmez.
    transient = [r for r in results if not r.ok and r.quality >= 70
                 and (r.error_kind in ("rate_limit", "server") or (r.error_kind == "quota" and r.ever_ok))
                 and roomy(r.ref)]
    transient = sorted(transient, key=lambda r: -r.quality)[:3]
    brain = brain[:limit - len(transient)] + transient
    # Küçük bağlamlı hızlı sağlayıcılar (Groq) da girer: istek sığmazsa router onları atlayıp sonrakine geçer.
    voice = [r for r in ok if r.quality >= 60]
    voice = _diversify(sorted(voice, key=lambda r: -voice_score(r)), voice_score)
    fast = [r for r in ok if r.quality >= 55 and r.latency <= 15]
    fast.sort(key=lambda r: -fast_score(r))
    vision = [r for r in ok if (pc := provider_cfg(r.ref)) and pc.supports_vision(r.ref.split(":", 1)[1])]
    vision.sort(key=lambda r: -brain_score(r))
    vision = vision[:4]
    vision += [next(r for r in ok if r.ref == ref) for ref in vision_backups(
        {r.ref: asdict(r) for r in ok}, providers_cfg, [r.ref for r in vision])]
    return {"brain": [r.ref for r in brain][:limit], "voice": [r.ref for r in voice][:6] + [r.ref for r in transient],
            "fast": [r.ref for r in fast][:6],
            "vision": [r.ref for r in vision]}


def refresh(router, emit: Callable[[str], None] = lambda s: None, path: Path | None = None,
            per_provider: int = PER_PROVIDER, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    refs, errors, catalog = candidates(router, per_provider)
    emit(f"{len(refs)} aday ölçülüyor ({', '.join(sorted({r.split(':', 1)[0] for r in refs}))})")
    results = probe_all(router, refs, emit, timeout)
    prev = (load_cache(path).get("results") or {})
    for r in results:
        old = prev.get(r.ref) or {}
        r.ever_ok = r.ok or bool(old.get("ever_ok") or old.get("ok"))
    results.sort(key=lambda r: (not r.ok, -brain_score(r)))
    cache = {
        "probed_at": datetime.now().isoformat(timespec="seconds"),
        "probed_ts": time.time(),
        "catalog_errors": errors,
        "catalog": catalog,
        "results": {r.ref: asdict(r) for r in results},
        "chains": build_chains(results, router.providers_cfg),
    }
    p = path or cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return cache


_refreshing = threading.Lock()


def refresh_in_background(router, emit: Callable[[str], None] = lambda s: None,
                          max_age: float = CACHE_MAX_AGE) -> bool:
    """Önbellek eskiyse (ve config "auto" kullanıyorsa) arka planda yenile, bitince router'ı güncelle."""
    wanted = uses_auto(router.config.roles) or router.config.agent.get("auto_models", True)
    if not wanted or cache_age(load_cache()) < max_age:
        return False
    if not _refreshing.acquire(blocking=False):
        return False

    def work() -> None:
        try:
            emit("modeller ölçülüyor (arka planda, günde bir)")
            cache = refresh(router)
            router.reload_auto(cache)
            emit("model sırası güncellendi: " + ", ".join(cache["chains"].get("brain", [])[:3]))
        except Exception as e:  # noqa: BLE001 - arka plan işi uygulamayı düşürmesin
            emit(f"model ölçümü başarısız: {e}")
        finally:
            _refreshing.release()

    threading.Thread(target=work, daemon=True, name="alice-model-probe").start()
    return True


def format_table(cache: dict[str, Any]) -> list[str]:
    rows = [f"{'model':58s} {'durum':10s} {'kalite':>6s} {'süre':>6s} {'ilk tk':>6s} {'token':>6s} {'tk/sn':>6s}"]
    for r in cache.get("results", {}).values():
        state = "OK" if r["ok"] else r["error_kind"][:10]
        rows.append(f"{r['ref'][:58]:58s} {state:10s} {r['quality']:6d} {r['latency']:6.1f} {r['ttft']:6.1f} "
                    f"{r['tokens']:6d} {r['tps']:6.1f}")
    return rows
