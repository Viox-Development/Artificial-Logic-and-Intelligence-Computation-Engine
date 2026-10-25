"""Rol tabanlı model router.

Her rol (brain, fast, vision) sıralı bir "provider:model" listesine sahip. İstek
listedeki ilk uygun modele gider; hata türüne göre:

  rate_limit -> o model kısa süre soğumaya alınır (retry-after ya da 20 sn)
  quota      -> günlük kota: sağlayıcının kotası sıfırlanana kadar (Gemini: Pasifik gece yarısı) kapanır ve
                bu süre ~/.alice/cooldowns.json'a yazılır, ALICE yeniden başlasa da aynı modele boşuna gidilmez;
                günlük olduğu anlaşılmayan kota hatası 1 saat soğuma
  dakikalık kota (Gemini "PerMinute") -> rate_limit gibi, Gemini'nin söylediği kadar bekler
  auth       -> sağlayıcı oturum boyunca kapatılır (key yanlış/eksik)
  not_found  -> model oturum boyunca kapatılır (katalogdan kalkmış)
  too_large  -> sadece bu istek için atlanır (context o sağlayıcıya sığmıyor)
  bad_request + tools -> model bu oturumda tool'suz işlerde kullanılır
  server/network -> 30 sn soğuma
  unreachable -> 10 dk soğuma (sunucu yok, ör. Ollama kapalı)

Ücretsiz katmanlar sürekli değiştiği için tek bir sağlayıcıya güvenmek yerine
zinciri bu şekilde dolaşmak sistemin ayakta kalmasını sağlıyor.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from alice.config import Config, ProviderConfig, alice_home
from alice.providers import context, ranking, thinking
from alice.providers.openai_compat import (ChatResult, OpenAICompatProvider, ProviderError, StreamCancelled,
                                            redact)

TIMEOUT_COOLDOWN = 600.0  # sn: zaman aşımına uğrayan model bu kadar atlanır
COOLDOWNS = {"rate_limit": 20.0, "quota": 3600.0, "server": 30.0, "network": 30.0, "unreachable": 600.0}


def seconds_until_reset(provider: str, base_url: str = "", now: datetime | None = None) -> float:
    """Günlük ücretsiz kotanın sıfırlanmasına kalan süre. Gemini kotaları Pasifik saatiyle gece yarısı,
    OpenRouter ve diğerleri UTC gece yarısı sıfırlanıyor. Bilinmeyen saat dilimi verisinde (Windows'ta tzdata
    yoksa) Pasifik için UTC-8 alınır: en fazla bir saat fazla beklenir."""
    now = now or datetime.now(timezone.utc)
    tz: Any = timezone.utc
    if provider == "gemini" or "generativelanguage.googleapis.com" in base_url:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo("America/Los_Angeles")
        except Exception:  # noqa: BLE001 - tzdata yok
            tz = timezone(timedelta(hours=-8))
    local = now.astimezone(tz)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60.0, (midnight - local).total_seconds() + 60)


def account_key(provider: str) -> str:
    """Hesap genelindeki günlük kotanın soğuma anahtarı (ör. OpenRouter'ın tüm :free modelleri tek kotada)."""
    return f"{provider}:*:free"


def parse_ref(ref: str) -> tuple[str, str]:
    provider, sep, model = ref.partition(":")
    if not sep or not model:
        raise ValueError(f"geçersiz model referansı: {ref!r} (beklenen 'provider:model')")
    return provider, model


def estimate_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
    chars = 0
    images = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for p in c:
                if p.get("type") == "text":
                    chars += len(p.get("text", ""))
                elif p.get("type") == "image_url":
                    images += 1
        for tc in m.get("tool_calls") or []:
            chars += len(tc["function"].get("arguments", "")) + 30
    if tools:
        chars += len(json.dumps(tools))
    return int(chars / 3.5) + images * 1100


def strip_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            texts = []
            for p in c:
                if p.get("type") == "text":
                    texts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    texts.append("[görüntü: bu model görsel desteklemiyor, atlandı]")
            m = {**m, "content": "\n".join(texts)}
        out.append(m)
    return out


# Gemini'nin belgelediği yer tutucu imza: başka bir modelin yaptığı ya da imzası kaybolmuş çağrılar için.
GEMINI_DUMMY_SIGNATURE = "skip_thought_signature_validator"


def is_gemini(pc: ProviderConfig) -> bool:
    return "generativelanguage.googleapis.com" in pc.base_url or pc.name == "gemini"


def clean_messages(messages: list[dict[str, Any]], gemini: bool = False) -> list[dict[str, Any]]:
    """ALICE'in iç işaretlerini ("_" ile başlayan anahtarlar) sağlayıcıya göndermeden önce temizle.
    Gemini'ye giderken her tool_call'a thought_signature eklenir (yoksa Gemini 3 isteği 400 ile reddediyor)."""
    out = []
    for m in messages:
        m2 = {k: v for k, v in m.items() if not k.startswith("_")}
        if m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                tc2 = {k: v for k, v in tc.items() if not k.startswith("_")}
                if gemini:
                    tc2["extra_content"] = tc.get("_extra_content") or {
                        "google": {"thought_signature": GEMINI_DUMMY_SIGNATURE}}
                calls.append(tc2)
            m2["tool_calls"] = calls
        out.append(m2)
    return out


def _has_images(messages: list[dict[str, Any]]) -> bool:
    return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
               for m in messages)


class StreamSink(Protocol):
    def on_delta(self, text: str) -> None: ...
    def on_reset(self) -> None: ...
    def cancelled(self) -> bool: ...


class AllProvidersFailed(Exception):
    def __init__(self, role: str, errors: list[str]):
        super().__init__(f"'{role}' rolündeki hiçbir model cevap veremedi:\n  " + "\n  ".join(errors))
        self.errors = errors


@dataclass
class UsageTracker:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def record(self, ref: str, usage: dict[str, Any], ok: bool) -> None:
        with self._lock:
            day = self.data.setdefault(date.today().isoformat(), {})
            row = day.setdefault(ref, {"requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0})
            if ok:
                row["requests"] += 1
                row["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                row["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            else:
                row["errors"] += 1
            # Sadece son 14 günü tut.
            for k in sorted(self.data)[:-14]:
                del self.data[k]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            except OSError:
                pass

    def today(self) -> dict[str, Any]:
        return self.data.get(date.today().isoformat(), {})


class Router:
    def __init__(
        self,
        config: Config,
        provider_factory: Callable[[ProviderConfig], Any] | None = None,
        usage_path: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_wait: float = 65.0,
        on_event: Callable[[str], None] | None = None,
        auto_cache: dict[str, Any] | None = None,
    ):
        self.config = config
        self.providers_cfg = config.providers()
        self._factory = provider_factory or (lambda pc: OpenAICompatProvider(pc))
        self._real_clients = provider_factory is None
        self._probe_checked = False
        self._clients: dict[str, Any] = {}
        self.cooldown_until: dict[str, float] = {}
        self.cooldown_kind: dict[str, str] = {}
        self.dead_models: set[str] = set()
        self.dead_providers: set[str] = set()
        self.no_tools: set[str] = set()
        self.no_thinking: set[str] = set()  # düşünme parametresini reddeden modeller
        self.thinking_overrides: dict[str, str] = {}  # /think ile oturumluk seviye
        self.size_limit: dict[str, int] = {}
        self.usage = UsageTracker(usage_path or alice_home() / "usage.json")
        self.sleep = sleep
        self.max_wait = max_wait
        self.on_event = on_event or (lambda s: None)
        self.last_ref: dict[str, str] = {}
        self.role_overrides: dict[str, list[str]] = {}
        self.log_path: Path | None = (usage_path.parent if usage_path else alice_home()) / "logs" / "providers.log"
        self.auto_cache: dict[str, Any] = ranking.load_cache() if auto_cache is None else auto_cache
        self.no_vision: set[str] = set()  # görsel desteklediği sanılıp görseli reddeden modeller
        self.cooldown_path: Path = (usage_path.parent if usage_path else alice_home()) / "cooldowns.json"
        self._load_cooldowns()

    def _load_cooldowns(self) -> None:
        """Önceki oturumda günlük kotası dolan modeller: süre dolmadıysa yine kapalı başlar."""
        try:
            saved = json.loads(self.cooldown_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        wall, mono = time.time(), time.monotonic()
        for ref, until in (saved or {}).items():
            try:
                left = float(until) - wall
            except (TypeError, ValueError):
                continue
            if left > 0:
                self.cooldown_until[ref] = mono + left
                self.cooldown_kind[ref] = "quota"

    def _save_cooldowns(self) -> None:
        wall, mono = time.time(), time.monotonic()
        data = {ref: round(wall + (until - mono)) for ref, until in self.cooldown_until.items()
                if self.cooldown_kind.get(ref) == "quota" and until - mono > 600}
        try:
            self.cooldown_path.parent.mkdir(parents=True, exist_ok=True)
            self.cooldown_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass

    def thinking_level(self, role: str) -> str:
        return self.thinking_overrides.get(role) or self.thinking_overrides.get("*") or thinking.level_for(
            self.config.data, role)

    # ------------------------------------------------------------------ helpers
    def chain(self, role: str) -> list[str]:
        """Rolün model sırası. "auto" ölçülmüş en iyi ücretsiz modellere açılır; ölçümde kalıcı hata veren
        (kalkmış, bu anahtarla kapalı) modeller atlanır; rastgele model seçen router'lar en sona itilir."""
        if role in self.role_overrides:
            pinned = self.role_overrides[role]
            if role not in self.config.roles or not self.config.agent.get("pinned_fallback", True):
                return pinned
            # Arayüzden seçilen model önce denenir; aşırı yük (503) ya da kota yüzünden cevap veremezse iş
            # durmasın diye rolün normal zinciriyle devam edilir.
            return pinned + [r for r in self._role_chain(role) if r not in pinned]
        return self._role_chain(role)

    def _role_chain(self, role: str) -> list[str]:
        if ":" in role and role.split(":", 1)[0] in self.providers_cfg:
            return [role]  # doğrudan model referansı
        raw = self.config.roles.get(role)
        if raw is None:
            raise KeyError(f"tanımsız rol: {role}")
        if "auto" not in raw and self.config.agent.get("auto_models", True):
            # Elle yazılmış zincirler de ölçülen modellerle desteklenir: elle yazılanlar önce, sonra ölçülenler.
            raw = list(raw) + ["auto"]
        expanded: list[str] = []
        for ref in raw:
            expanded += self.auto_chain(role) if ref == "auto" else [ref]
        dead = self._cached_dead()
        out: list[str] = []
        for ref in expanded:
            if ref not in out and ref not in dead:
                out.append(ref)
        shown = [r for r in out if not self.is_hidden(r)]
        out = shown or out  # hepsi gizlendiyse rol tamamen çalışamaz hale gelmesin
        return [r for r in out if r not in ranking.RANDOM_ROUTERS] + [r for r in out if r in ranking.RANDOM_ROUTERS]

    def auto_chain(self, role: str) -> list[str]:
        chains = self.auto_cache.get("chains") or {}
        refs = chains.get(role) or (role == "voice" and chains.get("brain")) or ranking.DEFAULT_AUTO.get(role, [])
        res = self.auto_cache.get("results") or {}
        if role == "brain" and res:  # önceki ölçümle kurulmuş zinciri güncel puan ağırlıklarına göre yeniden diz
            w = ranking.load_weights()
            # Sadece ölçümde çalışanlar puana göre dizilir; geçici hatalı yedekler (süresi 0 kaydedilmiş, "çok hızlı"
            # görünür) sonda kalır. Önceden yedek model çalışanların önüne geçiyordu.
            refs = sorted(refs, key=lambda r: (0, -ranking.role_score(ranking.with_context(res[r], r, self.providers_cfg),
                                                                      "brain", w)) if r in res and res[r].get("ok", True)
                          else (1, 0))
        if role == "voice" and not chains.get("voice"):  # eski ölçüm: beyin zincirini ses puanına göre diz
            w = ranking.load_weights()
            refs = sorted(refs or ranking.DEFAULT_AUTO["brain"],
                          key=lambda r: (0, -ranking.role_score(ranking.with_context(res[r], r, self.providers_cfg),
                                                                "voice", w)) if r in res and res[r].get("ok", True) else (1, 0))
        if role == "vision" and res:
            refs = list(refs) + ranking.vision_backups(res, self.providers_cfg, list(refs))
        return [r for r in refs if r.partition(":")[0] in self.providers_cfg]

    def _cached_dead(self) -> set[str]:
        if ranking.cache_age(self.auto_cache) > 7 * 24 * 3600:
            return set()
        dead = {ref for ref, r in (self.auto_cache.get("results") or {}).items()
                if r.get("error_kind") in ("not_found", "auth")}
        # Sağlayıcının kataloğunda artık olmayan (kalkmış, adı değişmiş) elle yazılmış modeller.
        catalog = self.auto_cache.get("catalog") or {}
        for chain in self.config.roles.values():
            for ref in chain or []:
                p, _, model = ref.partition(":")
                if catalog.get(p) and model not in catalog[p]:
                    dead.add(ref)
        return dead

    def hidden(self) -> set[str]:
        """Arayüzden gizlenen modeller ve sağlayıcılar ([agent] hidden_models): otomatik zincirlerde kullanılmaz."""
        return {str(x) for x in self.config.agent.get("hidden_models") or []}

    def is_hidden(self, ref: str) -> bool:
        h = self.hidden()
        return ref in h or ref.partition(":")[0] in h

    def set_hidden(self, name: str, hidden: bool) -> list[str]:
        cur = [str(x) for x in self.config.agent.get("hidden_models") or []]
        cur = [x for x in cur if x != name] + ([name] if hidden else [])
        self.config.data.setdefault("agent", {})["hidden_models"] = cur
        return cur

    def catalog_view(self) -> list[dict[str, Any]]:
        """Sağlayıcıya göre gruplanmış bütün bilinen modeller: ölçümde çalışanlar ve zincirlerde yazılı olanlar."""
        res = self.auto_cache.get("results") or {}
        dead = self._cached_dead()
        refs: set[str] = {r for r, v in res.items() if v.get("ok") or v.get("ever_ok")}
        for role in self.config.roles:
            try:
                refs.update(self.chain(role))
            except KeyError:
                pass
        hidden = self.hidden()
        out: list[dict[str, Any]] = []
        for name, pc in sorted(self.providers_cfg.items()):
            rows = []
            for ref in refs:
                if ref.partition(":")[0] != name or ref in dead:
                    continue
                r = res.get(ref) or {}
                rows.append({"ref": ref, "model": ref.partition(":")[2], "status": self.available(ref)[1],
                             "hidden": ref in hidden, "quality": r.get("quality", ranking.quality(ref)),
                             "latency": round(float(r.get("latency") or 0), 1), "tested": bool(r)})
            rows.sort(key=lambda x: (-x["quality"], x["latency"] or 99, x["ref"]))
            out.append({"name": name, "enabled": bool(pc.enabled), "hidden": name in hidden,
                        "key": pc.api_key_env, "models": rows})
        return out

    def reload_auto(self, cache: dict[str, Any] | None = None) -> None:
        self.auto_cache = ranking.load_cache() if cache is None else cache

    def _log(self, line: str) -> None:
        """Sağlayıcı hatalarını ~/.alice/providers.log'a yaz (1 MB'ı geçince bir eskisi saklanır)."""
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > 1_000_000:
                self.log_path.replace(self.log_path.with_suffix(".log.1"))
            with open(self.log_path, "a", encoding="utf-8") as f:
                # Sağlayıcıların anahtarları log dosyasına asla düşmesin (hata metni yankılamış olabilir).
                keys = [pc.api_key for pc in self.providers_cfg.values() if getattr(pc, "api_key", None)]
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(redact(line, *keys).split())}\n")
        except OSError:
            pass

    def client(self, provider: str) -> Any:
        if provider not in self._clients:
            self._clients[provider] = self._factory(self.providers_cfg[provider])
        return self._clients[provider]

    def close(self) -> None:
        """Açılan sağlayıcı istemcilerini (HTTP bağlantı havuzlarını) kapat. Sonra kullanılırsa yeniden açılır."""
        while self._clients:
            _, c = self._clients.popitem()
            close = getattr(c, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - kapanışta hata önemsiz
                    pass

    def supports_vision(self, ref: str) -> bool:
        p, m = parse_ref(ref)
        pc = self.providers_cfg.get(p)
        return bool(pc and pc.supports_vision(m)) and ref not in self.no_vision

    def available(self, ref: str, now: float | None = None) -> tuple[bool, str]:
        now = time.monotonic() if now is None else now
        try:
            p, _ = parse_ref(ref)
        except ValueError as e:
            return False, str(e)
        pc = self.providers_cfg.get(p)
        if pc is None:
            return False, "bilinmeyen sağlayıcı"
        if not pc.enabled:
            return False, f"{pc.api_key_env} yok"
        if p in self.dead_providers:
            return False, "sağlayıcı kapalı (auth)"
        if ref in self.dead_models:
            return False, "model bulunamadı"
        until = self.cooldown_until.get(ref, 0.0)
        if ref.endswith(":free"):
            until = max(until, self.cooldown_until.get(account_key(p), 0.0))
        if until > now:
            return False, f"soğumada ({int(until - now)} sn)"
        return True, "hazır"

    def context_limit(self, ref: str) -> int:
        """Modelin bağlam penceresi: bilinen modelde modelin kendisi, değilse sağlayıcı ayarı (bkz. context.py)."""
        p, model = parse_ref(ref)
        pc = self.providers_cfg.get(p)
        return context.context_limit(pc.max_input_tokens if pc else 32000, model, self.size_limit.get(ref))

    def first_available(self, role: str) -> str | None:
        for ref in self.chain(role):
            if self.available(ref)[0]:
                return ref
        return None

    # --------------------------------------------------------------------- chat
    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        stream: "StreamSink | None" = None,
        gemini_extra: dict[str, Any] | None = None,
    ) -> ChatResult:
        """stream verilirse cevap parça parça stream.on_delta'ya akar. Bir model akışın ortasında
        düşerse stream.on_reset() çağrılır ve sıradaki model baştan dener."""
        if not self._probe_checked:
            # Gerçek sağlayıcılarla çalışırken model ölçümü bir günden eskiyse arka planda yenilenir.
            self._probe_checked = True
            if self._real_clients:
                ranking.refresh_in_background(self, emit=self.on_event)
        chain = self.chain(role)
        est = estimate_tokens(messages, tools)
        attempts = 0
        failed: list[str] = []
        while True:
            errors: list[str] = []
            now = time.monotonic()
            for ref in chain:
                if stream is not None and stream.cancelled():
                    raise StreamCancelled()
                ok, why = self.available(ref, now)
                if not ok:
                    errors.append(f"{ref}: {why}")
                    continue
                p, model = parse_ref(ref)
                pc = self.providers_cfg[p]
                limit = self.context_limit(ref)
                if est > limit:
                    errors.append(f"{ref}: istek ~{est} token, model sınırı {limit}")
                    continue
                if tools and ref in self.no_tools:
                    errors.append(f"{ref}: tool desteklemiyor")
                    continue
                if est > self.size_limit.get(ref, 10**9):
                    errors.append(f"{ref}: istek ~{est} token, bu model daha önce ~{self.size_limit[ref]} üstünü reddetti")
                    continue
                vision = pc.supports_vision(model) and ref not in self.no_vision
                msgs = clean_messages(messages if vision else strip_images(messages),
                                      gemini=is_gemini(pc))
                client = self.client(p)
                think = {} if ref in self.no_thinking else thinking.params(p, model, self.thinking_level(role))
                extra = {**think, **(self.config.model_extra(ref) or {})}  # elle yazılan model ayarı önce gelir
                if gemini_extra and is_gemini(pc):  # ör. reasoning_effort: sadece Gemini'nin anladığı ayarlar
                    extra.update(gemini_extra)
                kw = dict(tools=tools, temperature=temperature, max_tokens=max_tokens,
                          response_format=response_format, extra=extra or None)
                emitted = [False]

                def call(kw=kw, client=client, model=model, msgs=msgs, emitted=emitted):
                    if stream is not None and hasattr(client, "chat_stream"):
                        def on_delta(piece: str, _flag=emitted, _sink=stream) -> None:
                            _flag[0] = True
                            _sink.on_delta(piece)

                        r = client.chat_stream(model, msgs, on_delta, cancel=stream.cancelled,
                                               on_reasoning=getattr(stream, "on_reasoning", None),
                                               **({"on_tool": stream.on_tool} if hasattr(stream, "on_tool") else {}), **kw)
                        if not r.usage:
                            r.usage = {"prompt_tokens": est, "completion_tokens": int(len(r.text) / 3.5)}
                        return r
                    r = client.chat(model, msgs, **kw)
                    if stream is not None and r.text:
                        stream.on_delta(r.text)
                    return r

                try:
                    try:
                        res = call()
                    except ProviderError as e:
                        if not (think and e.kind == "bad_request") or emitted[0]:
                            raise
                        # Sağlayıcı düşünme parametresini tanımadı: bir daha gönderme, ayarsız tekrar dene.
                        self.no_thinking.add(ref)
                        self._log(f"{ref} düşünme ayarını kabul etmedi ({e}); ayarsız deneniyor")
                        kw["extra"] = {k: v for k, v in extra.items() if k not in think} or None
                        res = call()
                except ProviderError as e:
                    if emitted[0]:
                        stream.on_reset()
                    self.usage.record(ref, {}, ok=False)
                    errors.append(f"{ref}: {e}")
                    self._penalize(ref, p, e, tools is not None, response_format is not None, est,
                                   images=vision and _has_images(msgs))
                    self._log(f"{role} {ref} ~{est}tk HATA {e}")
                    failed.append(f"{ref} ({e.status or e.kind})")
                    continue
                self.usage.record(ref, res.usage, ok=True)
                if failed:
                    # Arayüz bunu not olarak gösterir: hangi model düştü, kim devraldı.
                    msg = f"{role}: {', '.join(failed[-3:])} hata verdi, {ref} modeline geçildi"
                    self.on_event(msg)
                    self._log(msg)
                if self.last_ref.get(role) != ref and not role.startswith(("team:", "council:")):  # panel gösterir
                    self.on_event(f"{role} -> {ref}")
                self.last_ref[role] = ref
                return res

            # Hepsi başarısız. Sadece rate limit yüzünden kısa süre kapalı bir model varsa bekle.
            waits = [u - time.monotonic() for r, u in self.cooldown_until.items()
                     if r in chain and self.cooldown_kind.get(r) == "rate_limit"]
            waits = [w for w in waits if w > 0]
            attempts += 1
            if waits and min(waits) <= self.max_wait and attempts <= 3:
                w = min(waits) + 0.5
                self.on_event(f"tüm modeller meşgul, {w:.0f} sn bekleniyor")
                self.sleep(w)
                continue
            self._log(f"{role} TÜM MODELLER BAŞARISIZ: " + " | ".join(errors))
            raise AllProvidersFailed(role, errors)

    def _penalize(self, ref: str, provider: str, e: ProviderError, had_tools: bool, had_format: bool,
                  est: int = 0, images: bool = False) -> None:
        now = time.monotonic()
        low = str(e).lower()
        if e.kind == "quota":
            pc = self.providers_cfg.get(provider)
            daily = getattr(e, "daily", False)
            wait = seconds_until_reset(provider, pc.base_url if pc else "") if daily else COOLDOWNS["quota"]
            self.cooldown_until[ref] = now + wait
            self.cooldown_kind[ref] = "quota"
            if daily and provider in ranking.ACCOUNT_QUOTA and ref.endswith(":free"):
                self.cooldown_until[account_key(provider)] = now + wait
                self.cooldown_kind[account_key(provider)] = "quota"
            if daily:
                self._save_cooldowns()
            return
        if e.kind == "bad_request" and images and any(w in low for w in ("image", "vision", "multimodal", "modality")):
            # Görsel destekliyor sanılan model görseli reddetti: bu oturumda ona görsel gönderme, sıradakine geç.
            self.no_vision.add(ref)
            return
        if e.kind == "auth":
            self.dead_providers.add(provider)
        elif e.kind == "not_found":
            self.dead_models.add(ref)
        elif e.kind == "too_large":
            # Ör. Groq ücretsiz katmanı dakikada 8K token: bu büyüklükteki istekleri bir daha bu modele yollama.
            if est:
                self.size_limit[ref] = min(self.size_limit.get(ref, 10**9), int(est * 0.9))
        elif e.kind == "bad_request":
            if had_tools and "thought_signature" not in low and ("tool" in low or "function" in low):
                self.no_tools.add(ref)
            elif not had_format or had_tools:
                self.cooldown_until[ref] = now + 60
        else:
            base = COOLDOWNS.get(e.kind, 30.0)
            if e.kind == "network" and "zaman aşımı" in str(e):
                # Cevap vermeden takılan model (NVIDIA'da GLM-5.3 gibi) 30 sn sonra yine denenince her istek
                # zaman aşımını (180 sn) baştan bekliyordu: bir süre sıradakiyle devam et.
                base = TIMEOUT_COOLDOWN
            wait = base
            if e.retry_after and e.kind == "rate_limit":
                wait = e.retry_after
            elif e.retry_after:
                # 503 + Retry-After: sunucu ne zaman açılacağını söylüyor. Önceden yok sayılıp 30 sn sonra yine
                # aynı modele gidiliyor, her seferinde boşuna bir istek ve bekleme harcanıyordu.
                wait = max(base, e.retry_after)
            self.cooldown_until[ref] = now + min(max(wait, 1.0), 6 * 3600)
            self.cooldown_kind[ref] = e.kind

    def status(self) -> dict[str, list[tuple[str, str]]]:
        roles = set(self.config.roles) | set(self.role_overrides)
        return {role: [(ref, self.available(ref)[1]) for ref in self.chain(role)] for role in sorted(roles)}
