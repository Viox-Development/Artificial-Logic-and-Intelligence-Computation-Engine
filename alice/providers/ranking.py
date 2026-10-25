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
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home
from alice.providers import arena

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
    ("deepseek-v3", 84), ("qwen3-235b", 82), ("qwen3-32b", 70),
]
# Tabloda olmayan model: yine ölçülür (SambaNova gibi sağlayıcıların modelleri hiç denenmiyordu), ama zekâsı
# bilinmediği için bu puanla ana/ses zincirine (60+) girmez; ekranda "zekâ bilinmiyor" diye görünür.
UNKNOWN_QUALITY = 45
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


def table_quality(ref: str) -> int:
    """Elle yazılmış tablodan tahmin (LMArena'da bulunamayan modeller için)."""
    low = ref.lower()
    if any(x in low for x in EXCLUDE):
        return 0
    for pat, score in QUALITY:
        if pat in low:
            return score
    return 0


_offset: tuple[float, int] = (0.0, 0)  # (LMArena verisinin zamanı, tablo -> LMArena ölçek farkı)


def _table_offset() -> int:
    """Tablo puanları LMArena ölçeğinden cömert (gemini-3.8-flash 93'e 88, glm-5.3 90'a 81). İkisinde de olan
    modellerin farkının ortancası tablodaki tahminlere eklenir: iki kaynaktan gelen puanlar aynı ölçekte olsun."""
    global _offset
    data = arena.load()
    ts = float(data.get("fetched_ts") or 0)
    if _offset[0] == ts:
        return _offset[1]
    diffs = sorted(hit["q"] - score for pat, score in QUALITY
                   if (hit := (data.get("scores") or {}).get(arena.norm(pat))))
    off = round(diffs[len(diffs) // 2]) if len(diffs) >= 5 else 0
    _offset = (ts, off)
    return off


def quality(ref: str) -> int:
    """Zekâ puanı: LMArena'daki gerçek sıralama, bulunamazsa elle yazılmış tablo (aynı ölçeğe çekilmiş)."""
    low = ref.lower()
    if any(x in low for x in EXCLUDE):
        return 0
    hit = arena.lookup(ref)
    if hit is not None:
        return max(1, round(hit["q"]))
    q = table_quality(ref)
    return max(1, min(100, q + _table_offset())) if q else 0


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
    context: int = 0  # token: modelin bağlamı ile sağlayıcıya gönderilebilen sınırın küçüğü (0 = bilinmiyor)

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


def candidates(router, per_provider: int = PER_PROVIDER,
               contexts: dict[str, int] | None = None) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
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
            if q == 0 and not any(x in f.ref.lower() for x in EXCLUDE) and name != "openrouter":
                q = UNKNOWN_QUALITY  # OpenRouter'ın yüzlerce bilinmeyen modeli ölçümü boğmasın
            if q > 0 and f.tools is not False:
                by_provider.setdefault(name, []).append((q, f.ref))
                if contexts is not None and f.context:
                    contexts[f.ref] = int(f.context)
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
    from alice.providers.openai_compat import OpenAICompatProvider, ProviderError, redact
    from alice.providers.router import clean_messages, is_gemini, parse_ref

    p, model = parse_ref(ref)
    pc = router.providers_cfg[p]
    client = OpenAICompatProvider(pc, timeout=timeout)
    msgs = clean_messages([
        {"role": "system", "content": "You are ALICE, an agent on the user's Windows PC. Use tools to act."},
        {"role": "user", "content": PROBE_PROMPT},
    ], gemini=is_gemini(pc))
    res = Probe(ref, False, quality(ref) or UNKNOWN_QUALITY)
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
        res.error_kind, res.error = "network", redact(f"{type(e).__name__}: {e}", pc.api_key)[:300]
        return res
    finally:
        # Her ölçüm kendi httpx istemcisini açıyor (günde ~50 model): önceden hiç kapatılmıyor, bağlantılar ve
        # dosya tanıtıcıları arka plan iş parçacığında birikiyordu.
        client.close()
    total =time.monotonic() - t0
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
# Her model beş ölçütte 0-100 puan alır; rolün ağırlıklarıyla genel puan çıkar. Ağırlıklar uygulamada
# (Ayarlar → Modeller → Puanlama) değiştirilebilir, ~/.alice/model-weights.json'a yazılır.
METRICS = {"zeka": "zekâ (araç kullanma, Türkçe, kod)", "hiz": "hız (işi bitirme süresi)",
           "tepki": "ilk kelime süresi", "akis": "akış (saniyede token)",
           "baglam": "bağlam (ne kadar uzun konuşma, dosya, sayfa sığar)",
           "guven": "güvenilirlik (gerçek kullanımda takılmadan, saçmalamadan cevap verme)"}
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    # Ajan bir işte 10-20 kez model çağırıyor: zekâ önde ama yavaşlık ve takılma da pahalı. Önceden zekâ %60'tı:
    # çok zeki ama yavaş/takılan modeller (glm-5.3) öne geçip bir iş 5-10 dakika sürüyordu; şimdi ortası iyi olan.
    # Bağlam: ajan uzun dosya/sayfa okuyor, konuşma uzuyor; küçük bağlamlı model işin ortasında sığmaz olur.
    "brain": {"zeka": 45, "hiz": 20, "tepki": 10, "akis": 5, "baglam": 10, "guven": 10},
    # Sesli asistan: ilk kelimeye kadar geçen süre neredeyse zekâ kadar önemli; konuşmalar kısa.
    "voice": {"zeka": 40, "hiz": 10, "tepki": 35, "akis": 5, "baglam": 0, "guven": 10},
    # Hızlı işler (özet, sınıflandırma): yeterince iyi olsun, çabuk bitsin; özet için biraz bağlam.
    "fast": {"zeka": 30, "hiz": 45, "tepki": 10, "akis": 5, "baglam": 5, "guven": 5},
    # Görsel: ekran okuma işinde doğruluk önde; bir saniyelik fark daha az önemli.
    "vision": {"zeka": 60, "hiz": 15, "tepki": 5, "akis": 5, "baglam": 10, "guven": 5},
}

# ------------------------------------------------------------------ gerçek kullanımda güvenilirlik
# Ölçüm tek bir kısa istek: bir modelin uzun konuşmada takılıp takılmadığını göstermiyor. Router her gerçek
# çağrının sonucunu buraya yazar; takılan (zaman aşımı), saçmalayan, 5xx veren model puanda geriye düşer.
# Sayılar haftada yarıya iner: düzelen model zamanla eski yerine döner.
HEALTH_HALF_LIFE = 7 * 24 * 3600
BAD_KINDS = ("network", "garbled", "server")  # modelin kendi sorunu; kota/hız sınırı/istek hatası sayılmaz
UNRELIABLE = (3.0, 0.4)  # en az bu kadar hata ve güvenilirlik bunun altındaysa zincirin sonuna
_health: dict[str, dict[str, float]] | None = None
_health_lock = threading.Lock()
_health_dirty = 0


def health_path() -> Path:
    return alice_home() / "model-health.json"


def load_health() -> dict[str, dict[str, float]]:
    global _health
    with _health_lock:
        if _health is None:
            try:
                data = json.loads(health_path().read_text(encoding="utf-8"))
                _health = {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
            except (OSError, ValueError):
                _health = {}
        return _health


def _decayed(h: dict[str, float], now: float) -> tuple[float, float]:
    f = 0.5 ** (max(0.0, now - float(h.get("ts", now))) / HEALTH_HALF_LIFE)
    return float(h.get("ok", 0)) * f, float(h.get("bad", 0)) * f


SPEED_ALPHA = 0.3  # gerçek hız ölçümünde yeni çağrının ağırlığı (üstel ortalama)
SPEED_MIN = 3  # en az bu kadar gerçek çağrı olunca ölçüm yerine gerçek kullanımın hızı sayılır


def record_outcome(ref: str, ok: bool, kind: str = "", now: float | None = None, ttft: float = 0.0,
                   tps: float = 0.0) -> None:
    """Gerçek bir çağrının sonucu (başarılıysa ilk kelime süresi ve saniyede token). Hatalar hemen, başarılar
    ara ara diske yazılır."""
    global _health_dirty
    if not ok and kind not in BAD_KINDS:
        return
    now = time.time() if now is None else now
    health = load_health()
    with _health_lock:
        prev = health.get(ref, {})
        good, bad = _decayed(prev, now)
        row = {"ok": round(good + ok, 3), "bad": round(bad + (not ok), 3), "ts": now}
        for key, val in (("ttft", ttft), ("tps", tps)):
            old, n = prev.get(key), int(prev.get(key + "_n", 0))
            if ok and val > 0:
                row[key] = round(val if old is None else old + SPEED_ALPHA * (val - old), 3)
                row[key + "_n"] = n + 1
            elif old is not None:
                row[key], row[key + "_n"] = old, n
        health[ref] = row
        _health_dirty += 1
        if ok and _health_dirty < 20:
            return
        _health_dirty = 0
        snapshot = json.dumps(health, ensure_ascii=False)
    try:
        health_path().parent.mkdir(parents=True, exist_ok=True)
        health_path().write_text(snapshot, encoding="utf-8")
    except OSError:
        pass


def reliability(ref: str, now: float | None = None) -> tuple[float, float]:
    """(0-1 güvenilirlik, hata sayısı). Veri yoksa 1: ölçümde çalışan model suçsuz sayılır."""
    h = load_health().get(ref)
    if not h:
        return 1.0, 0.0
    good, bad = _decayed(h, time.time() if now is None else now)
    return (good + 2) / (good + bad + 2), bad


def live_speed(ref: str) -> dict[str, float]:
    """Gerçek kullanımda ölçülen ilk kelime süresi ve saniyede token (yeterince çağrı varsa). Günlük ölçüm tek bir
    kısa istek: şansa bağlı ve uzun konuşmadaki hızı göstermiyor."""
    h = load_health().get(ref) or {}
    out: dict[str, float] = {}
    for key in ("ttft", "tps"):
        if int(h.get(key + "_n", 0)) >= SPEED_MIN and h.get(key):
            out[key] = float(h[key])
    if out:
        out["live_calls"] = float(max(int(h.get("ttft_n", 0)), int(h.get("tps_n", 0))))
    return out


def unreliable(ref: str) -> bool:
    rel, bad = reliability(ref)
    return bad >= UNRELIABLE[0] and rel < UNRELIABLE[1]


def weights_path() -> Path:
    return alice_home() / "model-weights.json"


def load_weights() -> dict[str, dict[str, float]]:
    out = {role: dict(w) for role, w in DEFAULT_WEIGHTS.items()}
    try:
        saved = json.loads(weights_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for role, w in (saved if isinstance(saved, dict) else {}).items():
        if role in out and isinstance(w, dict):
            for k, v in w.items():
                if k in METRICS and isinstance(v, (int, float)) and 0 <= v <= 100:
                    out[role][k] = float(v)
    return out


def save_weights(weights: dict[str, dict[str, float]]) -> None:
    p = weights_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(weights, ensure_ascii=False, indent=1), encoding="utf-8")


def _get(r: Any, key: str) -> float:
    v = r.get(key) if isinstance(r, dict) else getattr(r, key, 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def context_of(ref: str, providers_cfg: dict[str, Any] | None, catalog: int | None = None) -> int:
    """Modelin kullanılabilir bağlamı: katalogdaki değer ile ALICE'in o sağlayıcıya gönderdiği sınırın küçüğü."""
    pc = (providers_cfg or {}).get(ref.partition(":")[0])
    limit = int(getattr(pc, "max_input_tokens", 0) or 0)
    vals = [v for v in (catalog, limit) if v]
    return min(vals) if vals else 0


def with_context(r: dict[str, Any], ref: str, providers_cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Eski ölçümde bağlam yok: sağlayıcının sınırıyla doldur. Gerçek kullanımdaki güvenilirlik de eklenir."""
    out = r if r.get("context") else {**r, "context": context_of(ref, providers_cfg)}
    out = {**out, "reliability": reliability(ref)[0]}
    q = quality(ref)  # LMArena güncellendiyse ölçüm anındaki eski puan yerine güncel olan
    if q:
        out["quality"] = q
    return {**out, **live_speed(ref)}


def metric_scores(r: Any) -> dict[str, float]:
    """Ölçüm sonucundan (Probe ya da önbellekteki sözlük) 0-100 alt puanlar. Eğriler yumuşak: 1 sn -> 2 sn farkı
    8 sn -> 9 sn farkından çok daha önemli."""
    lat, ttft, tps = _get(r, "latency"), _get(r, "ttft"), _get(r, "tps")
    return {
        "zeka": max(0.0, min(100.0, _get(r, "quality"))),
        "hiz": round(100 / (1 + max(0.0, lat - 1) / 4), 1),        # 1 sn 100, 3 sn 67, 5 sn 50, 13 sn 25
        "tepki": round(100 / (1 + max(0.0, ttft - 0.3) / 1.0), 1),  # 0,3 sn 100, 0,8 sn 67, 1,3 sn 50, 5 sn 18
        # 150 token/sn ve üstü 100. 0 = ölçülemedi (cevap tek parçada geldi): ceza değil, orta puan.
        "akis": round(min(100.0, tps / 1.5), 1) if tps > 0 else 50.0,
        # 8K 20, 16K 40, 32K 60, 64K 80, 128K ve üstü 100; bilinmiyorsa orta
        "baglam": (round(max(0.0, min(100.0, 20 * math.log2(ctx / 4000))), 1) if (ctx := _get(r, "context")) else 50.0),
        # Karesi: 2 başarı 2 takılma (%67) 44, hep takılan (%29) 8. Takılan model zekâsına rağmen geriye düşsün.
        "guven": round(100 * _rel(r) ** 2, 1),
    }


def _rel(r: Any) -> float:
    v = r.get("reliability") if isinstance(r, dict) else getattr(r, "reliability", None)
    return 1.0 if v is None else max(0.0, min(1.0, float(v)))


def role_score(r: Any, role: str = "brain", weights: dict[str, dict[str, float]] | None = None) -> float:
    w = (weights or load_weights()).get(role) or DEFAULT_WEIGHTS.get(role) or DEFAULT_WEIGHTS["brain"]
    total = sum(w.values()) or 1.0
    m = metric_scores(r)
    return round(sum(m[k] * w.get(k, 0) for k in METRICS) / total, 1)


def brain_score(pr: Probe, weights: dict[str, dict[str, float]] | None = None) -> float:
    return role_score(pr, "brain", weights)


def brain_score_of(quality: float, latency: float, ttft: float = 0.0, tps: float = 0.0,
                   weights: dict[str, dict[str, float]] | None = None) -> float:
    return role_score({"quality": quality, "latency": latency, "ttft": ttft, "tps": tps}, "brain", weights)


def fast_score(pr: Probe, weights: dict[str, dict[str, float]] | None = None) -> float:
    return role_score(pr, "fast", weights)


def voice_score(pr: Probe, weights: dict[str, dict[str, float]] | None = None) -> float:
    return role_score(pr, "voice", weights)


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
    for ref, r in sorted(results.items(),
                         key=lambda kv: -role_score(with_context(kv[1], kv[0], providers_cfg), "vision")):
        prov, _, model = ref.partition(":")
        pc = providers_cfg.get(prov)
        if (len(out) < limit and r.get("ok") and prov not in used and ref not in have and pc
                and pc.supports_vision(model) and quality(ref) > 0):
            used.add(prov)
            out.append(ref)
    return out


def build_chains(results: list[Probe], providers_cfg: dict[str, Any], limit: int = 10,
                 weights: dict[str, dict[str, float]] | None = None) -> dict[str, list[str]]:
    weights = weights or load_weights()

    def live(r: Probe) -> dict[str, Any]:
        return {**asdict(r), "reliability": reliability(r.ref)[0], **live_speed(r.ref)}

    # Gerçek kullanımda sık takılan model puanı ne olursa olsun çalışanların arkasına (yedek olarak kalır).
    def brain_score(r: Probe) -> float:
        return role_score(live(r), "brain", weights) - 1000 * unreliable(r.ref)

    def voice_score(r: Probe) -> float:
        return role_score(live(r), "voice", weights) - 1000 * unreliable(r.ref)

    def fast_score(r: Probe) -> float:
        return role_score(live(r), "fast", weights) - 1000 * unreliable(r.ref)

    def vision_score(r: Probe) -> float:
        return role_score(live(r), "vision", weights) - 1000 * unreliable(r.ref)

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
    vision.sort(key=lambda r: -vision_score(r))
    vision = vision[:4]
    vision += [next(r for r in ok if r.ref == ref) for ref in vision_backups(
        {r.ref: asdict(r) for r in ok}, providers_cfg, [r.ref for r in vision])]
    return {"brain": [r.ref for r in brain][:limit], "voice": [r.ref for r in voice][:6] + [r.ref for r in transient],
            "fast": [r.ref for r in fast][:6],
            "vision": [r.ref for r in vision]}


def refresh(router, emit: Callable[[str], None] = lambda s: None, path: Path | None = None,
            per_provider: int = PER_PROVIDER, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    if getattr(router, "_real_clients", False) and arena.age() > arena.MAX_AGE:
        arena.refresh(emit)  # zekâ puanları: gerçek benchmark (üç günde bir, birkaç saniye)
    contexts: dict[str, int] = {}
    refs, errors, catalog = candidates(router, per_provider, contexts=contexts)
    emit(f"{len(refs)} aday ölçülüyor ({', '.join(sorted({r.split(':', 1)[0] for r in refs}))})")
    results = probe_all(router, refs, emit, timeout)
    prev = (load_cache(path).get("results") or {})
    for r in results:
        old = prev.get(r.ref) or {}
        r.ever_ok = r.ok or bool(old.get("ever_ok") or old.get("ok"))
        r.context = context_of(r.ref, router.providers_cfg, contexts.get(r.ref))
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


def rebuild(router, weights: dict[str, dict[str, float]] | None = None, path: Path | None = None) -> dict[str, Any]:
    """Son ölçümün sonuçlarıyla zincirleri yeniden kur (yeni ağırlıklar): modelleri tekrar ölçmeden."""
    cache = load_cache(path)
    results = []
    for d in (cache.get("results") or {}).values():
        try:
            d = with_context(d, str(d.get("ref", "")), router.providers_cfg)
            results.append(Probe(**{k: v for k, v in d.items() if k in Probe.__dataclass_fields__}))
        except TypeError:
            continue
    if not results:
        return cache
    cache["chains"] = build_chains(results, router.providers_cfg, weights=weights)
    p = path or cache_path()
    try:
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    router.reload_auto(cache)
    return cache


def left_out(r: dict[str, Any], role: str, providers_cfg: dict[str, Any] | None) -> str:
    """Puanı yüksek olsa da model bu rolün sırasına neden girmiyor (build_chains'in koşulları)."""
    q, lat = float(r.get("quality") or 0), float(r.get("latency") or 0)
    prov, _, model = str(r.get("ref", "")).partition(":")
    pc = (providers_cfg or {}).get(prov)
    if not quality(str(r.get("ref", ""))):
        return "zekâ puanı bilinmiyor"
    if role in ("brain", "voice") and q < 60:
        return "zekâ 60'ın altında"
    if role == "brain" and pc is not None and pc.max_input_tokens < 16000:
        return f"bağlamı küçük ({pc.max_input_tokens // 1000}K), ana modelin isteğine sığmaz"
    if role == "fast" and (q < 55 or lat > 15):
        return "yeterince iyi ya da hızlı değil"
    if role == "vision" and pc is not None and not pc.supports_vision(model):
        return "görsel desteklemiyor"
    return "sıranın sonuna sığmadı"


def score_table(cache: dict[str, Any] | None = None, weights: dict[str, dict[str, float]] | None = None,
                providers_cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Ekrandaki puan tablosu: her model için alt puanlar, rol başına genel puan ve sıraya girmediyse nedeni."""
    cache = load_cache() if cache is None else cache
    weights = weights or load_weights()
    chains = cache.get("chains") or {}
    rows = []
    for ref, r in (cache.get("results") or {}).items():
        r = with_context(r, ref, providers_cfg)
        hit = arena.lookup(ref)
        row = {"ref": ref, "context": r.get("context") or 0, "ok": bool(r.get("ok")), "error": r.get("error_kind") or "",
               "known": quality(ref) > 0, "arena": {"name": hit["name"], "elo": hit["elo"]} if hit else None,
               "live_calls": int(r.get("live_calls") or 0), "latency": r.get("latency"), "ttft": r.get("ttft"), "tps": r.get("tps"),
               "metrics": metric_scores(r)}
        row["scores"] = {role: role_score(r, role, weights) for role in DEFAULT_WEIGHTS} if row["ok"] else {}
        row["why"] = {role: left_out({**r, "ref": ref}, role, providers_cfg) for role in DEFAULT_WEIGHTS
                      if row["ok"] and ref not in (chains.get(role) or [])}
        rows.append(row)
    rows.sort(key=lambda x: (not x["ok"], -(x["scores"].get("brain") or 0)))
    return rows


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
