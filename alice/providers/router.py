"""Role-based model router.

Each role (brain, fast, vision) has an ordered list of "provider:model" entries. A request goes to the first
suitable model in the list; depending on the error type:

  rate_limit -> that model is cooled down briefly (retry-after or 20 s)
  quota      -> daily quota: closed until the provider's quota resets (Gemini: Pacific midnight), and
                written to ~/.alice/cooldowns.json, so the same model is not retried in vain after a restart;
                a quota error not identified as daily gets a 1 hour cooldown
  per-minute quota (Gemini "PerMinute") -> like rate_limit, waits as long as Gemini says
  auth       -> provider is closed for the session (key wrong/missing)
  not_found  -> model is closed for the session (removed from the catalog)
  too_large  -> skipped for this request only (context doesn't fit that provider)
  bad_request + tools -> model is used for tool-less work in this session
  server/network -> 30 s cooldown (timeout 10 min)
  consecutive errors -> cooldown doubles each time (max 6 hours), reset on the first success
  reply hit max_tokens with no content -> the same model is retried once right away with a larger output budget
  unreachable -> 10 min cooldown (no server, e.g. Ollama not running)

Free tiers change constantly, so instead of relying on a single provider, walking the chain this way keeps
the system running.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from alice.config import Config, ProviderConfig, alice_home
from alice.i18n import _
from alice.providers import context, menu, ranking, thinking
from alice.providers.anthropic_api import THINKING_KEY
from alice.providers.chatgpt import make_client
from alice.providers.openai_compat import (ChatResult, OpenAICompatProvider, ProviderError, StreamCancelled,
                                            redact)

TIMEOUT_COOLDOWN = 600.0  # seconds: a model that timed out is skipped for this long
COOLDOWNS = {"rate_limit": 20.0, "quota": 3600.0, "server": 30.0, "network": 30.0, "unreachable": 600.0,
             "garbled": 1800.0}
# Letter ranges: if three of these are well represented in a reply, the model has collapsed and is producing
# nonsense text (e.g. glm-4.7-flash, in long contexts, started mixing up and inventing Turkish, Chinese and
# Greek words).
SCRIPTS = (("latin", 0x0041, 0x024F), ("latin", 0x1E00, 0x1EFF), ("greek", 0x0370, 0x03FF),
           ("cyrillic", 0x0400, 0x04FF), ("hebrew", 0x0590, 0x05FF), ("arabic", 0x0600, 0x06FF),
           ("devanagari", 0x0900, 0x097F), ("thai", 0x0E00, 0x0E7F), ("cjk", 0x3040, 0x30FF),
           ("cjk", 0x4E00, 0x9FFF), ("hangul", 0xAC00, 0xD7AF))
SCRIPT_MIN = 8  # letters needed for an alphabet to count as "present" (so stray symbols or names don't count)


def looks_garbled(text: str) -> bool:
    """Returns True if the reply mixes three or more alphabets (code blocks are not counted)."""
    plain = re.sub(r"```.*?(```|$)|`[^`]*`|https?://\S+", " ", text, flags=re.S)
    counts: dict[str, int] = {}
    for ch in plain:
        if not ch.isalpha():
            continue
        o = ord(ch)
        for name, lo, hi in SCRIPTS:
            if lo <= o <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
    return sum(n >= SCRIPT_MIN for n in counts.values()) >= 3


def seconds_until_reset(provider: str, base_url: str = "", now: datetime | None = None) -> float:
    """Seconds left until the daily free quota resets. Gemini quotas reset at midnight Pacific time,
    OpenRouter and others at midnight UTC. If timezone data is unavailable (no tzdata on Windows), UTC-8 is used
    for Pacific: at most one extra hour is waited."""
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


# Why a model can't be used right now. The reasons go into errors the model and logs see, so they are English;
# older builds wrote them in Turkish ("soğumada (N sn)"), and parsers accept both.
READY = ("ready", "ready (chosen by hand)")
COOLING_RX = re.compile(r"(?:cooling down|soğumada) \((\d+) (?:s|sn)\)")


def is_ready(why: str) -> bool:
    return why in READY or why in ("hazır", "hazır (elle seçildi)")


def reason_text(why: str) -> str:
    """A reason from Router.available in the interface language, for the UI and the terminal."""
    if m := COOLING_RX.search(why):
        return _("cooling down ({secs} s)", secs=m.group(1))
    if why.startswith("no ") and " " not in why[3:]:
        return _("no {key}", key=why[3:])
    return {"ready": _("ready"), "ready (chosen by hand)": _("ready (chosen by hand)"), "unknown provider": _("unknown provider"),
            "provider off (auth)": _("provider off (auth)"), "model not found": _("model not found")}.get(why, why)


def account_key(provider: str) -> str:
    """Cooldown key for the account-wide daily quota (e.g. all of OpenRouter's :free models share one quota)."""
    return f"{provider}:*:free"


def _hidden_match(ref: str, hidden: set[str]) -> bool:
    """Whether it is hidden: full name, provider name, or pattern ("cliproxy:kimi-*": all Kimi models on the proxy)."""
    if ref in hidden or ref.partition(":")[0] in hidden:
        return True
    from fnmatch import fnmatchcase

    return any("*" in h and fnmatchcase(ref, h) for h in hidden)


def _short_error(err: str) -> str:
    """Readable part of a measurement error: '[quota] {"error":{"message":"…"}}' -> the message."""
    m = re.search(r'\\?"message\\?"\s*:\s*\\?"((?:[^"\\]|\\[^"])*)', err)  # the record may be truncated
    if m:
        try:
            return json.loads(f'"{m.group(1)}"')[:300]
        except ValueError:
            return m.group(1)[:300]
    return re.sub(r"^\[\w+\]\s*", "", err).strip()[:300]


def parse_ref(ref: str) -> tuple[str, str]:
    provider, sep, model = ref.partition(":")
    if not sep or not model:
        raise ValueError(f"invalid model reference: {ref!r} (expected 'provider:model')")
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
                    texts.append("[image: this model does not support images, skipped]")
            m = {**m, "content": "\n".join(texts)}
        out.append(m)
    return out


# Gemini's documented placeholder signature: for calls made by another model or whose signature was lost.
GEMINI_DUMMY_SIGNATURE = "skip_thought_signature_validator"


def is_gemini(pc: ProviderConfig) -> bool:
    return "generativelanguage.googleapis.com" in pc.base_url or pc.name == "gemini"


_ACCEPTS: dict[tuple[Any, str], bool] = {}
# Request fields that change the provider's prompt cache key (thinking / effort settings of every provider).
CACHE_KEY_SETTINGS = ("reasoning_effort", "output_config", "reasoning", "chat_template_kwargs", "thinking")


def _accepts(fn: Any, name: str) -> bool:
    """Does fn accept this keyword argument (or everything via **kw)? Don't send an argument unknown to
    fake/old clients."""
    key = (getattr(fn, "__func__", fn), name)
    if key not in _ACCEPTS:
        import inspect

        try:
            params = inspect.signature(fn).parameters
            _ACCEPTS[key] = name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            _ACCEPTS[key] = False
    return _ACCEPTS[key]


def clean_messages(messages: list[dict[str, Any]], gemini: bool = False, claude: bool = False) -> list[dict[str, Any]]:
    """Strip ALICE's internal markers (keys starting with "_") before sending to the provider. The Claude API client
    (claude=True) also gets the thinking blocks it has to send back.
    When sending to Gemini, a thought_signature is added to each tool_call
    (otherwise Gemini 3 rejects the request with 400)."""
    out = []
    for m in messages:
        if m.get("_error") or m.get("_display_only"):
            continue  # provider error shown to the user ("Seçtiğin model cevap veremedi…"): not part of the conversation
        m2 = {k: v for k, v in m.items() if not k.startswith("_") or (claude and k == THINKING_KEY)}
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
    def __init__(self, role: str, errors: list[str], pinned: bool = False):
        head = (_("The model you chose couldn't answer; I didn't switch because you picked it by hand. "
                  "Pick another model from the model menu, or choose Auto:") if pinned
                else _("No model for the '{role}' role could answer:", role=role))
        super().__init__(head + "\n  " + "\n  ".join(errors))
        self.errors = errors
        self.pinned = pinned


def cached_tokens(usage: dict[str, Any]) -> int:
    """Input tokens read from the cache; providers report them under different names."""
    for path in (("prompt_tokens_details", "cached_tokens"), ("input_tokens_details", "cached_tokens"),
                 ("cached_tokens",), ("cache_read_input_tokens",), ("prompt_cache_hit_tokens",)):
        v: Any = usage
        for k in path:
            v = v.get(k) if isinstance(v, dict) else None
        if isinstance(v, (int, float)) and v:
            return int(v)
    return 0


def cache_write_tokens(usage: dict[str, Any]) -> int:
    """Input tokens written to the cache (Anthropic: cache_creation_input_tokens; cliproxy: cache_write_tokens)."""
    d = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    for v in (usage.get("cache_creation_input_tokens"), d.get("cache_write_tokens"), d.get("cached_creation_tokens")):
        if isinstance(v, (int, float)) and v:
            return int(v)
    return 0


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
                row["prompt_tokens"] += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                row["completion_tokens"] += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                # Show cache hits in daily usage too (which model reads how much from the cache).
                row["cached_tokens"] = row.get("cached_tokens", 0) + cached_tokens(usage)
                row["cache_write_tokens"] = row.get("cache_write_tokens", 0) + cache_write_tokens(usage)
            else:
                row["errors"] += 1
            # Keep only the last 14 days.
            for k in sorted(self.data)[:-14]:
                del self.data[k]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            except OSError:
                pass  # usage is kept in memory and saved on the next call

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
        self._factory = provider_factory or make_client
        self._real_clients = provider_factory is None
        self._subscription_models: list[str] | None = None  # used in tests instead of the proxy's model list
        self._probe_checked = False
        self._clients: dict[str, Any] = {}
        self._catalog_cache: dict[str, tuple[float, set[str]]] = {}  # provider -> (time, chat model refs)
        self.cooldown_until: dict[str, float] = {}
        self.cooldown_kind: dict[str, str] = {}
        self.dead_models: set[str] = set()
        self.dead_providers: set[str] = set()
        self.no_tools: set[str] = set()
        self.no_thinking: set[str] = set()  # models that reject the thinking parameter
        self.thinking_overrides: dict[str, str] = {}  # per-session level set with /think
        self.ultracode = False  # substantive work runs as workflows; until ALICE restarts (not saved)
        self.size_limit: dict[str, int] = {}
        self.strikes: dict[str, int] = {}  # consecutive errors: so we don't wait on the same model in vain
        self.out_floor: dict[str, int] = {}  # minimum output budget for a model that exhausts max_tokens while thinking
        self.usage = UsageTracker(usage_path or alice_home() / "usage.json")
        self.sleep = sleep
        self.max_wait = max_wait
        self.on_event = on_event or (lambda s: None)
        self.last_ref: dict[str, str] = {}
        self.last_settings: dict[str, dict[str, Any]] = {}
        self.role_overrides: dict[str, list[str]] = {}
        self.log_path: Path | None = (usage_path.parent if usage_path else alice_home()) / "logs" / "providers.log"
        self.auto_cache: dict[str, Any] = ranking.load_cache() if auto_cache is None else auto_cache
        self.no_vision: set[str] = set()  # models assumed to support vision that rejected an image
        self.cooldown_path: Path = (usage_path.parent if usage_path else alice_home()) / "cooldowns.json"
        self._load_cooldowns()

    def _load_cooldowns(self) -> None:
        """Models whose daily quota ran out in a previous session: if the time has not passed,
        they start closed again."""
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
            pass  # cooldowns are kept in memory for this process

    def thinking_level(self, role: str) -> str:
        return self.thinking_overrides.get(role) or self.thinking_overrides.get("*") or thinking.level_for(
            self.config.data, role)

    # ------------------------------------------------------------------ helpers
    def chain(self, role: str) -> list[str]:
        """The model order for a role. "auto" expands to the best measured free models; models that failed
        permanently in measurement (retired, or closed for this key) are skipped; routers that pick a random
        model are pushed to the very end."""
        if role in self.role_overrides:
            pinned = self.role_overrides[role]
            if role not in self.config.roles or not self.config.agent.get("pinned_fallback", False):
                # If the manually chosen model cannot answer, ALICE stops and says why. It used to silently fall
                # back to the role's chain: with Opus selected, the work went on with a very weak model like flash-lite.
                return pinned
            # [agent] pinned_fallback = true: try the chosen model first; if it fails, continue with the role's chain.
            return pinned + [r for r in self._role_chain(role) if r not in pinned]
        return self._role_chain(role)

    def brain_floor(self) -> int:
        """Minimum intelligence score to enter the main model's auto order ([agent] min_brain_quality)."""
        try:
            return int(self.config.agent.get("min_brain_quality", ranking.BRAIN_MIN_QUALITY))
        except (TypeError, ValueError):
            return ranking.BRAIN_MIN_QUALITY

    def strong_enough(self, refs: list[str]) -> list[str]:
        """Those of the automatically found models that are strong enough for main work. If none passes the threshold
        (a setup with only small models), the list is kept as is, so ALICE does not become completely unusable."""
        floor = self.brain_floor()
        if floor <= 0:
            return refs
        from alice.providers import picker

        good = [r for r in refs if picker.quality(r) >= floor]
        return good or refs

    def _role_chain(self, role: str) -> list[str]:
        if ":" in role and role.split(":", 1)[0] in self.providers_cfg:
            return [role]  # direct model reference
        raw = self.config.roles.get(role)
        if raw is None:
            raise KeyError(f"undefined role: {role}")
        if "auto" not in raw and self.config.agent.get("auto_models", True):
            # Hand-written chains are also backed by measured models: hand-written ones first, then the measured ones.
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
        out = shown or out  # if all are hidden, the role must not become completely unusable
        return [r for r in out if r not in ranking.RANDOM_ROUTERS] + [r for r in out if r in ranking.RANDOM_ROUTERS]

    def auto_chain(self, role: str) -> list[str]:
        chains = self.auto_cache.get("chains") or {}
        refs = chains.get(role) or (role == "voice" and chains.get("brain")) or ranking.DEFAULT_AUTO.get(role, [])
        res = self.auto_cache.get("results") or {}
        if role == "brain" and res:  # re-sort the last measured chain by current score weights
            w = ranking.load_weights()
            # Only models that worked in measurement are sorted by score; temporarily failing backups (saved with
            # a time of 0, so they look "çok hızlı" (too fast)) stay last. A backup used to come before working models.
            refs = sorted(refs, key=lambda r: (int(ranking.unreliable(r)), -ranking.role_score(
                ranking.with_context(res[r], r, self.providers_cfg), "brain", w)) if r in res and res[r].get("ok", True)
                          else (2, 0))
        if role == "voice" and not chains.get("voice"):  # old measurement: sort the brain chain by the voice score
            w = ranking.load_weights()
            refs = sorted(refs or ranking.DEFAULT_AUTO["brain"],
                          key=lambda r: (int(ranking.unreliable(r)), -ranking.role_score(
                              ranking.with_context(res[r], r, self.providers_cfg), "voice", w))
                          if r in res and res[r].get("ok", True) else (2, 0))
        if role == "vision" and res:
            refs = list(refs) + ranking.vision_backups(res, self.providers_cfg, list(refs))
        refs = [r for r in refs if r.partition(":")[0] in self.providers_cfg]
        if role == "brain":
            # When the strong models' quota runs out, the work falls to small models like flash-lite, gemma and step-5,
            # and they could not handle long agent work either. Rather than talk nonsense with a weak model, it is
            # right to stop and say "uygun model kalmadı" (no suitable model left).
            refs = self.strong_enough(refs)
            subs = self.subscription_chain()
            if subs:
                if any(r.startswith("cliproxy:gpt-") for r in subs):
                    # A ChatGPT account is linked to the proxy: its direct login must not enter the order twice.
                    refs = [r for r in refs if not r.startswith("chatgpt:")]
                refs = subs + [r for r in refs if r not in subs]
        return refs

    SUBSCRIPTION_ORDER = ("claude", "codex", "antigravity")
    SUBSCRIPTION_PER_GROUP = 2
    # Antigravity accounts expose models from multiple families (Gemini, Claude, GPT) under a single
    # subscription; allow more models from that group so the best of each family can enter the chain.
    SUBSCRIPTION_ANTIGRAVITY_PER_GROUP = 4

    def subscription_chain(self) -> list[str]:
        """Models from the subscription proxy (CLIProxyAPI) head the main model's automatic order: Claude first,
        then ChatGPT, then Antigravity ([agent] subscription_order). The proxy itself switches between accounts of
        the same service; if all of a service's accounts are full or failing, the next service is used. The few
        strongest models of each service are taken; a service with no usable account is skipped.

        Antigravity accounts may also serve Claude & GPT models under their own quota; these are grouped under
        "antigravity" (not "claude"/"codex") so they use the correct limits and account switching."""
        cp = self.providers_cfg.get("cliproxy")
        if cp is None or not cp.enabled or not (self._real_clients or self._subscription_models is not None):
            return []
        from alice.providers import picker

        try:
            from alice.app import cliproxy

            models = (self._subscription_models if self._subscription_models is not None
                      else cliproxy.tidy_models(cliproxy.live_models()))
            ready = {} if self._subscription_models is not None else cliproxy.groups_ready()
        except Exception:  # noqa: BLE001 - proxy is off: no subscription order
            return []
        order = [str(x) for x in self.config.agent.get("subscription_order") or self.SUBSCRIPTION_ORDER]
        floor = self.brain_floor()
        out: list[str] = []
        for gid in order:
            if ready.get(gid) is False:
                continue  # all accounts of this service are off / over quota / asking for a new login
            refs = [f"cliproxy:{m}" for m in models if cliproxy.model_group(m)[0] == gid]
            refs = [r for r in refs if not self.is_hidden(r) and not ranking.excluded(r)]
            # With equal scores, the newer version goes first (gpt-6.1-terra before gpt-5.6-terra).
            refs.sort(key=lambda r: (-picker.quality(r), [-int(x) for x in re.findall(r"\d+", r)]))
            # Those above the threshold come first. So the service is not left out of the order entirely,
            # the remaining slots are filled with its best models.
            good = [r for r in refs if picker.quality(r) >= floor]
            per = self.SUBSCRIPTION_ANTIGRAVITY_PER_GROUP if gid == "antigravity" else self.SUBSCRIPTION_PER_GROUP
            out += (good + [r for r in refs if r not in good])[:per]
        return out

    def _cached_dead(self) -> set[str]:
        if ranking.cache_age(self.auto_cache) > 7 * 24 * 3600:
            return set()
        dead = {ref for ref, r in (self.auto_cache.get("results") or {}).items()
                if r.get("error_kind") in ("not_found", "auth")}
        # Manually written models that are no longer in the provider's catalog (retired or renamed).
        catalog = self.auto_cache.get("catalog") or {}
        for chain in self.config.roles.values():
            for ref in chain or []:
                p, _sep, model = ref.partition(":")
                if catalog.get(p) and model not in catalog[p]:
                    dead.add(ref)
        return dead

    def hidden(self) -> set[str]:
        """Models and providers hidden from the UI ([agent] hidden_models): not used in automatic chains."""
        return {str(x) for x in self.config.agent.get("hidden_models") or []}

    def is_hidden(self, ref: str) -> bool:
        return _hidden_match(ref, self.hidden())

    def set_hidden(self, name: str, hidden: bool) -> list[str]:
        cur = [str(x) for x in self.config.agent.get("hidden_models") or []]
        cur = [x for x in cur if x != name] + ([name] if hidden else [])
        self.config.data.setdefault("agent", {})["hidden_models"] = cur
        return cur

    CATALOG_TTL = 600.0  # live catalog cache: opening the menu must not hit every provider
    CATALOG_TIMEOUT = 8.0  # a stuck provider must not hold the menu for minutes

    def _fetch_catalog(self, name: str) -> list[dict[str, Any]]:
        """The catalog is read on a throwaway client with a short timeout, not on the shared chat client: on a
        timeout the request is really cut off instead of trailing behind and keeping the chat client busy. The
        provider's own API style is used (including ones that are not OpenAI-compatible, like Anthropic)."""
        from alice.providers.openai_compat import OpenAICompatProvider

        pc = self.providers_cfg[name]
        if pc.kind == "anthropic":
            from alice.providers.anthropic_api import AnthropicProvider

            client = AnthropicProvider(pc, timeout=self.CATALOG_TIMEOUT)
        else:
            client = OpenAICompatProvider(pc, timeout=self.CATALOG_TIMEOUT)
        try:
            return client.list_models()
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    def worker_models(self, ref: str, most: int = 4) -> list[str]:
        """Other current models of the same provider, for the cheaper stages of a workflow. Only from the catalog that
        was already read: this runs while a message is being sent."""
        provider, _sep, model = ref.partition(":")
        rows = [{"ref": r, "model": r.partition(":")[2], "ok": self.available(r)[0], "hidden": self.is_hidden(r),
                 "quality": ranking.quality(r)} for r in (self._catalog_cache.get(provider) or (0, ()))[1]
                if not menu.disguised(r.partition(":")[2])]
        entry = {"name": provider, "enabled": True, "hidden": False, "models": rows,
                 "native": getattr(self.providers_cfg.get(provider), "kind", "") == "anthropic"}
        menu.mark([entry])
        return [r["ref"] for r in rows if r["main"] and r["ok"] and r["model"] != model][:most]

    def _catalog_refs(self, names: list[str]) -> set[str]:
        """The providers' chat models, briefly from cache. Missing ones are read in parallel; if one cannot be
        read the menu opens with its measured models, the error goes to the log and the next open retries."""
        import concurrent.futures

        from alice.providers.discovery import _generic, _openrouter

        now = time.monotonic()
        refs: set[str] = set()
        missing = []
        for name in names:
            hit = self._catalog_cache.get(name)
            if hit and now - hit[0] < self.CATALOG_TTL:
                refs.update(hit[1])
            else:
                missing.append(name)
        if not missing:
            return refs
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(missing))
        futs = {pool.submit(self._fetch_catalog, name): name for name in missing}
        try:
            done, not_done = concurrent.futures.wait(futs, timeout=self.CATALOG_TIMEOUT + 2)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        for fut in not_done:
            name = futs[fut]
            self._log(f"catalog unreadable ({name}): timeout")
        for fut in done:
            name = futs[fut]
            try:
                models = fut.result()
            except Exception as e:  # noqa: BLE001 - an unreadable catalog leaves the menu on the measured models
                self._log(f"catalog unreadable ({name}): {e}")
                continue
            found = _openrouter(models) if name == "openrouter" else _generic(name, models)
            if name == "openrouter":
                found = [f for f in found if ranking.quality(f.ref)]
            got = {f.ref for f in found}
            self._catalog_cache[name] = (now, got)
            refs.update(got)
        return refs

    def catalog_view(self) -> list[dict[str, Any]]:  # noqa: C901
        """All known models grouped by provider: those that worked in measurement and those written in the chains."""
        res = self.auto_cache.get("results") or {}
        dead = self._cached_dead()
        refs: set[str] = {r for r, v in res.items() if v.get("ok") or v.get("ever_ok")}
        refs.update(r for chain in self.role_overrides.values() for r in chain)  # mark chosen model
        for name, pc in self.providers_cfg.items():
            # Paid/manual models (Claude API) and user-added custom providers (e.g. 'evren') with an explicit models list
            if (not pc.auto or pc.custom) and pc.enabled and pc.models:
                refs.update(f"{name}:{m}" for m in pc.models)
        if getattr(self, "_real_clients", False):
            # Every provider without a fixed model list contributes its live catalog: the menu must not stay tied
            # to the measurement cache, and models added to the API later must show up too. The results are cached
            # for a while so the menu does not go to the network on every open. OpenRouter's hundreds of models
            # would drown the menu, so its catalog is limited to free models with a known score (QUALITY table or
            # LMArena).
            names = [name for name, pc in self.providers_cfg.items()
                     if pc.enabled and not getattr(pc, "models", None)
                     and name not in ("ollama", "cliproxy", "chatgpt")]
            refs.update(self._catalog_refs(names))
        cp = self.providers_cfg.get("cliproxy")
        if cp is not None and cp.enabled and getattr(self, "_real_clients", False):
            try:  # real model list of the accounts connected to the proxy (Kimi, Antigravity, new accounts)
                from alice.app.cliproxy import live_models, tidy_models

                refs.update(f"cliproxy:{m}" for m in tidy_models(live_models()))
            except Exception:  # noqa: BLE001
                pass
        cg = self.providers_cfg.get("chatgpt")
        if cg is not None and cg.auth == "chatgpt" and cg.enabled:
            from alice.providers.chatgpt import cached_models, ensure_models

            ensure_models(cg)  # read in background if missing; menu shows it on next open
            refs.update(f"chatgpt:{m}" for m in cached_models())  # selectable after login, even before measurement
        for role in self.config.roles:
            try:
                refs.update(self.chain(role))
            except KeyError:
                pass  # role without a chain: nothing to add
        hidden = self.hidden()
        refs = {r for r in refs if not menu.disguised(r.partition(":")[2])}
        out: list[dict[str, Any]] = []
        for name, pc in sorted(self.providers_cfg.items()):
            rows = []
            for ref in refs:
                if ref.partition(":")[0] != name or ref in dead:
                    continue
                r = res.get(ref) or {}
                ok, why = self.available(ref)
                rows.append({"ref": ref, "model": ref.partition(":")[2], "status": reason_text(why), "ok": ok,
                             "hidden": ref in hidden or (_hidden_match(ref, hidden) and name == "cliproxy"),
                             "quality": r.get("quality", ranking.quality(ref)),
                             "latency": round(float(r.get("latency") or 0), 1), "tested": bool(r)})
            rows.sort(key=lambda x: (-x["quality"], x["latency"] or 99, x["ref"]))
            entry = {"name": name, "enabled": bool(pc.enabled), "hidden": name in hidden,
                     "key": pc.api_key_env, "models": rows, "native": pc.kind == "anthropic"}
            if not rows and pc.enabled:
                # No model works, but it was tried: show the provider's error. Before, a server added from the UI
                # (e.g. OrcaRouter, whose free models are closed for this account) said "eklendi" (added),
                # yet never appeared in the list.
                failed = [v for r, v in res.items() if r.partition(":")[0] == name and not v.get("ok")]
                if failed:
                    entry["error"] = _short_error(str(failed[-1].get("error") or ""))
            if pc.auth == "chatgpt":
                from alice.providers.chatgpt import limit_note, models_error, models_state

                # State is read first, then the error. If the background list request finished between the two reads
                # (used to read the error first), both looked empty: the menu showed neither models nor error.
                state = models_state() if pc.enabled and not rows else ""
                entry.update(key=_("ChatGPT sign-in"), note=limit_note() if pc.enabled else "",
                             error=models_error() if pc.enabled and not rows else "",
                             loading=state == "loading")
            out.append(entry)
        out = self._split_proxy(out, hidden)
        menu.mark(out, keep=set(self.role_overrides.get("brain") or ()))
        return out

    @staticmethod
    def _split_proxy(out: list[dict[str, Any]], hidden: set[str] | None = None) -> list[dict[str, Any]]:
        """Split the Claude Code proxy's models by provider (Claude, ChatGPT, Kimi, Gemini, Antigravity...)
        instead of one single "cliproxy" pile. If the proxy has a standalone ChatGPT (codex) account, the group
        of the direct ChatGPT login is not shown in the menu: the same account used to appear under two separate
        "ChatGPT" headings, half under one and half under the other.

        Antigravity accounts may expose Claude & GPT models under Antigravity's own quota; model_group() uses
        the owned_by mapping to attribute those to the "antigravity" group, so they are not mixed with separate
        Claude/ChatGPT subscriptions."""
        idx = next((i for i, e in enumerate(out) if e["name"] == "cliproxy"), None)
        if idx is None or not out[idx]["models"]:
            return out
        from alice.app.cliproxy import model_group

        proxy = out.pop(idx)
        # Only hide the direct ChatGPT login group when a standalone codex subscription exists
        # (not when Claude/GPT models come from Antigravity).
        if any(model_group(row["model"])[0] == "codex" for row in proxy["models"]):
            out[:] = [e for e in out if e["name"] != "chatgpt"]
            idx = min(idx, len(out))
        groups: dict[str, dict[str, Any]] = {}
        for row in proxy["models"]:
            gid, label, pattern = model_group(row["model"])
            g = groups.setdefault(gid, {**{k: v for k, v in proxy.items() if k not in ("models", "error")},
                                         "name": pattern or "cliproxy", "label": label, "via": "cliproxy",
                                         "models": []})
            g["models"].append(row)
        for g in groups.values():
            g["hidden"] = bool(proxy["hidden"] or g["name"] in (hidden or set()))
        out[idx:idx] = list(groups.values())
        return out

    def reload_auto(self, cache: dict[str, Any] | None = None) -> None:
        self.auto_cache = ranking.load_cache() if cache is None else cache

    def _log(self, line: str) -> None:
        """Write provider errors to ~/.alice/providers.log (once it passes 1 MB, one old copy is kept)."""
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > 1_000_000:
                self.log_path.replace(self.log_path.with_suffix(".log.1"))
            with open(self.log_path, "a", encoding="utf-8") as f:
                # Provider keys must never end up in the log file (the error text may have echoed them).
                keys = [k for pc in self.providers_cfg.values() for k in getattr(pc, "api_keys", None) or []]
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(redact(line, *keys).split())}\n")
        except OSError:
            pass  # logging must not break a request

    def _log_cache(self, role: str, ref: str, usage: dict[str, Any] | None, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None) -> None:
        """One line per successful request in ~/.alice/logs/cache.log: how much of the prompt was read from / written
        to the cache. providers.log only kept errors, so prefix breaks (whole context rewritten) were invisible.
        Columns: time role ref prompt cached write msgs tools tools_hash system_hash."""
        if self.log_path is None:
            return
        try:
            import hashlib

            u = usage or {}
            prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
            names = ",".join(str((t.get("function") or t).get("name") or "") for t in tools or [])
            sys0 = messages[0].get("content") if messages and messages[0].get("role") == "system" else ""
            h = lambda s: hashlib.sha1(str(s).encode("utf-8", "replace")).hexdigest()[:8]  # noqa: E731
            path = self.log_path.parent / "cache.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 2_000_000:
                path.replace(path.with_suffix(".log.1"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {role} {ref} prompt={prompt} "
                        f"cached={cached_tokens(u)} write={cache_write_tokens(u)} msgs={len(messages)} "
                        f"tools={len(tools or [])} th={h(names)} sh={h(sys0)}\n")
        except (OSError, TypeError, ValueError, AttributeError):
            pass  # cache diagnostics must not break a request

    def client(self, provider: str) -> Any:
        if provider not in self._clients:
            from alice.providers.keypool import client_for

            self._clients[provider] = client_for(self.providers_cfg[provider], self._factory,
                                                 str(self.config.agent.get("key_strategy") or "round-robin"))
        return self._clients[provider]

    def key_status(self) -> dict[str, list[dict[str, Any]]]:
        """Key status of providers with more than one key (for the Anahtarlar (Keys) page in the UI)."""
        from alice.config import mask_secret
        from alice.providers.keypool import is_pool

        out: dict[str, list[dict[str, Any]]] = {}
        for name, pc in self.providers_cfg.items():
            keys = pc.api_keys
            if len(keys) < 2:
                continue
            c = self._clients.get(name)
            if is_pool(c) and len(c.keys) == len(keys):
                out[name] = c.status()
            else:
                out[name] = [{"index": i + 1, "preview": mask_secret(k), "state": "ready", "uses": 0, "errors": 0,
                              "waiting": {}, "error": ""} for i, k in enumerate(keys)]
        return out

    def reload_providers(self, name: str | None = None) -> None:
        """When a key or provider is added or removed: re-read the settings and rebuild the client from scratch."""
        self.providers_cfg = self.config.providers()
        for n in [name] if name else list(self._clients):
            c = self._clients.pop(n, None) if n else None
            close = getattr(c, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
            if n:
                self.dead_providers.discard(n)

    def close(self) -> None:
        """Close the opened provider clients (their HTTP connection pools). They are reopened if used later."""
        while self._clients:
            _name, c = self._clients.popitem()
            close = getattr(c, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - errors on close do not matter
                    pass

    def supports_vision(self, ref: str) -> bool:
        p, m = parse_ref(ref)
        pc = self.providers_cfg.get(p)
        return bool(pc and pc.supports_vision(m)) and ref not in self.no_vision

    def available(self, ref: str, now: float | None = None, pinned: bool = False) -> tuple[bool, str]:
        """pinned: the user picked this model by hand. Cooldown and per-session disabling only affect the automatic
        order; a manually chosen model is still tried on every request (timeouts used to block it for minutes)."""
        now = time.monotonic() if now is None else now
        try:
            p, _model = parse_ref(ref)
        except ValueError as e:
            return False, str(e)
        pc = self.providers_cfg.get(p)
        if pc is None:
            return False, "unknown provider"
        if not pc.enabled:
            return False, f"no {pc.api_key_env}"
        if pinned:
            return True, "ready (chosen by hand)"
        if p in self.dead_providers:
            return False, "provider off (auth)"
        if ref in self.dead_models:
            return False, "model not found"
        until = self.cooldown_until.get(ref, 0.0)
        if ref.endswith(":free"):
            until = max(until, self.cooldown_until.get(account_key(p), 0.0))
        if until > now:
            return False, f"cooling down ({int(until - now)} s)"
        return True, "ready"

    def context_limit(self, ref: str) -> int:
        """The model's context window: its own value if known, else the provider setting (see context.py)."""
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
        cache: dict[str, Any] | None = None,
    ) -> ChatResult:
        """If stream is given, the reply flows piece by piece into stream.on_delta. If a model fails mid-stream,
        stream.on_reset() is called and the next model starts over.
        cache: prompt cache hints for clients that place markers themselves (Claude API): {"ttl": "5m"|"1h",
        "mode": "write"|"read_only"|"none"}. Other providers ignore them."""
        if not self._probe_checked:
            # With real providers, model measurements older than a day are refreshed in the background.
            self._probe_checked = True
            if self._real_clients:
                ranking.refresh_in_background(self, emit=self.on_event)
        chain = self.chain(role)
        chosen = set(self.role_overrides.get(role) or [])  # hand-picked ones are tried even in cooldown
        est = estimate_tokens(messages, tools)
        attempts = 0
        failed: list[str] = []
        while True:
            errors: list[str] = []
            now = time.monotonic()
            for ref in chain:
                if stream is not None and stream.cancelled():
                    raise StreamCancelled()
                ok, why = self.available(ref, now, pinned=ref in chosen)
                if not ok:
                    errors.append(f"{ref}: {why}")
                    continue
                p, model = parse_ref(ref)
                pc = self.providers_cfg[p]
                limit = self.context_limit(ref)
                if est > limit:
                    errors.append(f"{ref}: request ~{est} tokens, model limit {limit}")
                    continue
                if tools and ref in self.no_tools:
                    errors.append(f"{ref}: does not support tools")
                    continue
                if est > self.size_limit.get(ref, 10**9):
                    errors.append(f"{ref}: request ~{est} tokens, this model earlier rejected anything over "
                                  f"~{self.size_limit[ref]}")
                    continue
                vision = pc.supports_vision(model) and ref not in self.no_vision
                msgs = clean_messages(messages if vision else strip_images(messages),
                                      gemini=is_gemini(pc), claude=pc.kind == "anthropic")
                client = self.client(p)
                # If the model lacks the selected level, use the nearest supported one (survives a model switch).
                # Thinking rules follow the API format, not the provider's name: a second Claude endpoint
                # ([providers.cliproxy-claude] kind = "anthropic") speaks output_config.effort like the Claude API.
                tp = "anthropic" if pc.kind == "anthropic" else p
                level = thinking.fit(self.thinking_level(role), thinking.levels_for(tp, model)[0])
                think = {} if ref in self.no_thinking else thinking.params(tp, model, level)
                extra = {**think, **(self.config.model_extra(ref) or {})}  # hand-written model settings come first
                if gemini_extra and is_gemini(pc):  # e.g. reasoning_effort: only settings Gemini understands
                    extra.update(gemini_extra)
                latch = (cache or {}).get("latch")
                if isinstance(latch, dict) and ref in self.no_thinking:
                    latch.pop(ref, None)  # the model rejected the thinking settings: never put them back
                elif isinstance(latch, dict):
                    # Sticky effort: the thinking/effort settings are part of the prompt cache key (Claude renders them
                    # into the prompt; changing output_config.effort invalidates the message cache, on some models the
                    # tool and system cache too). Once a conversation has sent them to a model, it keeps sending the
                    # same values to that model, even if routing moves the turn to a role with another level. The agent
                    # clears the latch when the user changes the level or the conversation is compacted.
                    keys = [k for k in CACHE_KEY_SETTINGS if k in extra or k in latch.get(ref, {})]
                    if ref in latch:
                        for k in keys:
                            if k in latch[ref]:
                                extra[k] = latch[ref][k]
                            else:
                                extra.pop(k, None)
                    else:
                        latch[ref] = {k: extra[k] for k in keys if k in extra}
                if cache and pc.kind == "anthropic":
                    if cache.get("ttl"):
                        extra["_cache_ttl"] = cache["ttl"]
                    if cache.get("mode"):
                        extra["_cache_mode"] = cache["mode"]
                    if cache.get("context_edit"):
                        extra["_context_edit"] = cache["context_edit"]
                # Request settings that are part of the cache key (for the cache break detector).
                self.last_settings[role] = {k: v for k, v in extra.items() if not str(k).startswith("_")}
                out = max(max_tokens, self.out_floor.get(ref, 0)) if max_tokens else None
                kw = dict(tools=tools, temperature=temperature, max_tokens=out,
                          response_format=response_format, extra=extra or None)
                emitted = [False]
                first = [0.0]  # first-word time (for measuring speed in real use)
                t_start = time.monotonic()

                def call(kw=kw, client=client, model=model, msgs=msgs, emitted=emitted, first=first):
                    if stream is not None and hasattr(client, "chat_stream"):
                        def on_delta(piece: str, _flag=emitted, _sink=stream) -> None:
                            _flag[0] = True
                            _sink.on_delta(piece)

                        def on_first(_first=first) -> None:
                            _first[0] = time.monotonic()

                        extra_kw = {"on_tool": stream.on_tool} if hasattr(stream, "on_tool") else {}
                        if isinstance(client, OpenAICompatProvider):
                            extra_kw["on_first"] = on_first
                        if hasattr(stream, "on_call") and _accepts(client.chat_stream, "on_call"):
                            extra_kw["on_call"] = stream.on_call  # finished tool call reported before the reply ends
                        r = client.chat_stream(model, msgs, on_delta, cancel=stream.cancelled,
                                               on_reasoning=getattr(stream, "on_reasoning", None), **extra_kw, **kw)
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
                        if emitted[0]:
                            raise
                        if think and e.kind == "bad_request":
                            # Provider rejected the thinking parameter: don't send it again, retry without it.
                            self.no_thinking.add(ref)
                            self._log(f"{ref} rejected the thinking setting ({e}); retrying without it")
                            kw["extra"] = {k: v for k, v in extra.items() if k not in think} or None
                        elif (e.kind == "too_large" and "max_tokens" in str(e) and out
                              and min(8192, max(out * 4, 1024)) > out):
                            # Cerebras qwen (thinking model) used up the small output budget and came back empty;
                            # the input isn't large, so widen the budget, retry this model once more, and remember it.
                            self.out_floor[ref] = min(8192, max(out * 4, 1024))
                            kw["max_tokens"] = self.out_floor[ref]
                            self._log(f"{ref} could not finish within max_tokens={out}; retrying with "
                                      f"{kw['max_tokens']}")
                        else:
                            raise
                        res = call()
                    if looks_garbled(res.text):
                        raise ProviderError("garbled", "the model produced garbled text mixing several languages")
                except ProviderError as e:
                    if emitted[0]:
                        stream.on_reset()
                    self.usage.record(ref, {}, ok=False)
                    if self._real_clients:
                        ranking.record_outcome(ref, False, e.kind)
                    errors.append(f"{ref}: {e}")
                    self._penalize(ref, p, e, tools is not None, response_format is not None, est,
                                   images=vision and _has_images(msgs))
                    self._log(f"{role} {ref} ~{est}tk HATA {e}")
                    failed.append(f"{ref} ({e.status or e.kind})")
                    continue
                self.usage.record(ref, res.usage, ok=True)
                self._log_cache(role, ref, res.usage, msgs, tools)
                self.strikes.pop(ref, None)
                if self._real_clients:
                    ttft = first[0] - t_start if first[0] else 0.0
                    gen = time.monotonic() - (first[0] or t_start)
                    out_tokens = float((res.usage or {}).get("completion_tokens") or 0)
                    ranking.record_outcome(ref, True, ttft=ttft,
                                           tps=out_tokens / gen if first[0] and gen > 0.2 and out_tokens >= 20 else 0.0)
                # A retried same model used to be logged as "X hata verdi, X modeline geçildi" (X failed, moved to X).
                others = [f for f in failed if f.rpartition(" (")[0] != ref]
                if others:
                    # The UI shows this as a note: which model failed, and who took over.
                    msg = _("{role}: {failed} failed, switched to {ref}", role=role, failed=", ".join(others[-3:]), ref=ref)
                    self.on_event(msg)
                    self._log(msg)
                if self.last_ref.get(role) != ref and not role.startswith(("team:", "council:")):  # the panel shows this
                    self.on_event(f"{role} -> {ref}")
                self.last_ref[role] = ref
                return res

            # All failed. Wait only if a model is paused briefly by a rate limit.
            # Pinned models (e.g. pinned_brain) or proxy pools should wait longer rather than bailing immediately.
            is_pinned = role in self.role_overrides and role in self.config.roles
            waits = [u - time.monotonic() for r, u in self.cooldown_until.items()
                     if r in chain and self.cooldown_kind.get(r) in ("rate_limit", "quota")]
            waits = [w for w in waits if w > 0]
            attempts += 1
            max_attempts = 5 if is_pinned else 3
            wait_limit = max(self.max_wait, 60.0) if is_pinned else self.max_wait
            if waits and min(waits) <= wait_limit and attempts <= max_attempts:
                w = min(waits) + 0.5
                self.on_event(_("all models are busy, waiting {s} s", s=f"{w:.0f}"))
                self.sleep(w)
                continue
            self._log(f"{role} ALL MODELS FAILED: " + " | ".join(errors))
            raise AllProvidersFailed(role, errors, pinned=role in self.role_overrides and role in self.config.roles)

    def _penalize(self, ref: str, provider: str, e: ProviderError, had_tools: bool, had_format: bool,
                  est: int = 0, images: bool = False) -> None:
        now = time.monotonic()
        low = str(e).lower()
        if e.kind == "quota":
            if provider == "cliproxy" or provider.startswith("cliproxy-"):
                # The proxy fronts a pool of accounts (e.g. multiple Antigravity, Claude, or Grok accounts).
                # On the first quota hit, use a brief pause (5s) to allow CLIProxy's round-robin / failover
                # to switch to another available account in the pool. If consecutive errors accumulate,
                # back off exponentially (up to 180s) indicating all accounts in the pool are exhausted.
                n = self.strikes.get(ref, 1)
                if n <= 1 and not e.retry_after:
                    wait = 5.0  # quick failover to sibling credential in proxy pool
                else:
                    wait = min(max(float(e.retry_after or (30.0 * 2 ** min(n - 1, 3))), 15.0), 180.0)
                self.cooldown_until[ref] = now + wait
                self.cooldown_kind[ref] = "quota"
                return
            pc = self.providers_cfg.get(provider)
            daily = getattr(e, "daily", False)
            wait = seconds_until_reset(provider, pc.base_url if pc else "") if daily else COOLDOWNS["quota"]
            if e.retry_after and not daily:
                # The provider said when it reopens (ChatGPT: resets_in_seconds). It used to be ignored.
                # Never shorten the wait: retrying an account that opens in 15 days every 6 hours was pointless.
                # (All accounts full: the provider already gives a short wait; cooldown clears when an account changes.)
                wait = min(max(float(e.retry_after), 60.0), 30 * 86400)
            self.cooldown_until[ref] = now + wait
            self.cooldown_kind[ref] = "quota"
            if daily and provider in ranking.ACCOUNT_QUOTA and ref.endswith(":free"):
                self.cooldown_until[account_key(provider)] = now + wait
                self.cooldown_kind[account_key(provider)] = "quota"
            if daily:
                self._save_cooldowns()
            return
        if e.kind == "bad_request" and images and any(w in low for w in ("image", "vision", "multimodal", "modality")):
            # A model believed to support images rejected one: don't send it images this session, move to the next.
            self.no_vision.add(ref)
            return
        if e.kind == "auth" and (provider == "cliproxy" or provider.startswith("cliproxy-")):
            # The proxy fronts many services and accounts: when the Claude login dropped, ChatGPT and
            # Antigravity models were also shut off for the whole session. Now only this one is skipped for a while.
            self.cooldown_until[ref] = now + 900
            self.cooldown_kind[ref] = "auth"
        elif e.kind == "auth":
            self.dead_providers.add(provider)
        elif e.kind == "not_found":
            self.dead_models.add(ref)
        elif e.kind == "too_large" and "max_tokens" in low:
            # Even the wide budget failed: the model is the problem, not the input. A flat 5-min penalty, in a job
            # running every 20 min kept re-testing Cerebras qwen, burning 8192 thinking tokens (Nov 1). Now exponential.
            n = self.strikes[ref] = self.strikes.get(ref, 0) + 1
            self.cooldown_until[ref] = now + min(300 * 2 ** min(n - 1, 6), 6 * 3600)
            self.cooldown_kind[ref] = "server"
        elif e.kind == "too_large":
            # e.g. Groq's free tier allows 8K tokens per minute: don't send requests this size to this model again.
            if est:
                self.size_limit[ref] = min(self.size_limit.get(ref, 10**9), int(est * 0.9))
        elif e.kind == "bad_request":
            if had_tools and "thought_signature" not in low and ("tool" in low or "function" in low):
                self.no_tools.add(ref)
            elif not had_format or had_tools:
                self.cooldown_until[ref] = now + 60
        else:
            base = COOLDOWNS.get(e.kind, 30.0)
            if e.kind == "network" and ("zaman aşımı" in str(e) or "timed out" in str(e)):
                # A model that hangs without replying (GLM-5.3 on NVIDIA), retried after 30 s, made every request
                # kept waiting out the full timeout (180 s) each time: for a while, carry on with the next one.
                base = TIMEOUT_COOLDOWN
            n = self.strikes[ref] = self.strikes.get(ref, 0) + 1
            wait = base * 2 ** min(n - 1, 5)  # GLM-5.3 was retried every 10 min, each time costing a 3 min wait
            if e.retry_after and e.kind == "rate_limit":
                # The provider's time is the basis; but if 429 still comes once it passes (Gemini quota gone,
                # it says "23 sn sonra dene", i.e. retry in 23 s): we kept hitting the same model; it now escalates on repeats.
                wait = max(e.retry_after, base * 2 ** min(n - 2, 5)) if n > 1 else e.retry_after
            elif e.retry_after:
                # 503 + Retry-After: the server says when it reopens. It used to be ignored and retried after 30 s, again
                # aynı modele gidiliyor, her seferinde boşuna bir istek ve bekleme harcanıyordu.
                wait = max(wait, e.retry_after)
            self.cooldown_until[ref] = now + min(max(wait, 1.0), 6 * 3600)
            self.cooldown_kind[ref] = e.kind

    def status(self) -> dict[str, list[tuple[str, str]]]:
        roles = set(self.config.roles) | set(self.role_overrides)
        return {role: [(ref, self.available(ref)[1]) for ref in self.chain(role)] for role in sorted(roles)}


class ChatRouter(Router):
    """One chat's view of the shared router. The model, effort and ultracode choices (and the model that last served,
    shown in the status bar) belong to the chat; everything else (clients, health, cooldowns, quota, catalogs) is the
    shared router's, read and written through. Before this, every chat used the app's router, so a model or effort
    picked in one chat changed all of them.

    A copy (copy.copy, as subagents and workflow agents make) stays a view of the same shared router."""

    OWN = frozenset({"role_overrides", "thinking_overrides", "ultracode", "last_ref", "last_settings", "on_event"})

    def __init__(self, base: Router) -> None:  # noqa: D107 - the shared router is not built again
        self.__dict__["_base"] = base
        self.__dict__["on_event"] = base.on_event
        self.reset()

    def reset(self) -> None:
        """Back to the defaults for a new chat: the shared router's choices (the model kept from the menu)."""
        base = self.__dict__["_base"]
        d = self.__dict__
        d["role_overrides"] = {k: list(v) for k, v in base.role_overrides.items()}
        d["thinking_overrides"] = dict(base.thinking_overrides)
        d["ultracode"] = bool(base.ultracode)
        d["last_ref"] = {}
        d["last_settings"] = {}

    def __getattr__(self, name: str) -> Any:
        base = self.__dict__.get("_base")
        if base is None or name.startswith("__"):  # copy.copy probes __setstate__ on an empty instance
            raise AttributeError(name)
        return getattr(base, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.OWN or name in self.__dict__:
            self.__dict__[name] = value
        else:
            setattr(self.__dict__["_base"], name, value)

    def export(self) -> dict[str, Any]:
        """The chat's own choices, saved with the chat."""
        return {"model": list(self.role_overrides.get("brain") or []),
                "thinking": self.thinking_overrides.get("brain", ""), "ultracode": bool(self.ultracode)}

    def restore(self, saved: Any) -> None:
        """A reopened chat gets its own choices back; a chat saved before they were kept uses the defaults."""
        if not isinstance(saved, dict):
            return
        from alice.providers import thinking

        model = [str(r) for r in saved.get("model") or [] if isinstance(r, str) and ":" in r]
        if "model" in saved:
            if model and model[0].partition(":")[0] in self.providers_cfg:
                self.role_overrides["brain"] = model[:1]
            elif not model:
                self.role_overrides.pop("brain", None)  # Automatic was picked in this chat
        level = str(saved.get("thinking") or "")
        if level in thinking.LEVELS:
            self.thinking_overrides["brain"] = level
        self.ultracode = bool(saved.get("ultracode"))
