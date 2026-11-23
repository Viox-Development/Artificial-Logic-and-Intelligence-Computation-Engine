"""Local CLIProxyAPI: account list, connecting new accounts (OAuth link), deleting accounts.

CLIProxyAPI runs in the `~/.alice/cliproxy` directory (config.yaml, auths/, keys.txt). The
"Claude Code proxy" card in the ALICE UI is fed from here: the user selects a service in the app and clicks the
login link, and the account is added in the browser. If multiple accounts are added, the proxy distributes requests
between them; when one hits a limit, it automatically switches to the next (CLIProxyAPI's default round-robin +
failover behavior).
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from alice.config import alice_home

TIMEOUT = 6.0

# Services displayed for "add account" in the UI (those supported by CLIProxyAPI for OAuth).
PROVIDERS: tuple[tuple[str, str], ...] = (
    ("claude", "Claude (Pro / Max)"),
    ("codex", "ChatGPT (Codex)"),
    ("antigravity", "Google Antigravity"),
    ("kimi", "Kimi"),
    ("xai", "xAI (Grok)"),
)
PROVIDER_NAMES = dict(PROVIDERS)
FILE_PREFIXES = (("claude-", "claude"), ("codex-", "codex"), ("antigravity-", "antigravity"),
                 ("kimi-", "kimi"), ("xai-", "xai"), ("meta-", "meta"), ("devin-", "devin"),
                 ("vertex-", "vertex"))


def home() -> Any:
    """CLIProxyAPI directory (`~/.alice/cliproxy`)."""
    return alice_home() / "cliproxy"


def binary_name() -> str:
    """Executable file name according to the platform."""
    return "cli-proxy-api.exe" if os.name == "nt" else "cli-proxy-api"


def settings() -> dict[str, Any]:
    """Directory, port, and management key. If not installed, installed=False; if no key, key=""."""
    h = home()
    port = 8317
    try:
        text = (h / "config.yaml").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"(?m)^\s*port:\s*(\d+)", text)
        if m:
            port = int(m.group(1))
    except OSError:
        pass  # no config yet: default port
    key = ""
    try:
        for line in (h / "keys.txt").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("management_key="):
                key = line.split("=", 1)[1].strip()
    except OSError:
        pass  # no keys file yet: no management key
    return {"dir": h, "port": port, "key": key,
            "installed": (h / binary_name()).exists()}


MODELS_TTL = 120.0  # s
_SKIP_MODELS = ("image", "embedding", "codex-auto-review", "tts")
_models_cache: dict[str, Any] = {"at": 0.0, "ids": [], "owners": {}}


def live_models(fetch: Any = None) -> list[str]:
    """Chat models currently offered by the proxy (/v1/models; varies by connected accounts). The hardcoded list in
    config only contained Claude/GPT: models of Kimi or subsequently connected accounts were never visible in the menu.
    If the proxy is down or does not respond, the last known list (initially empty) is returned."""
    now = time.monotonic()
    if _models_cache["at"] and now - _models_cache["at"] < MODELS_TTL:
        return list(_models_cache["ids"])
    _models_cache["at"] = now  # do not retry on every menu open even if there is an error
    try:
        if fetch is None:
            s = settings()
            key = ""
            for line in (s["dir"] / "keys.txt").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("api_key="):
                    key = line.split("=", 1)[1].strip()
            with httpx.Client(timeout=2.5) as c:
                r = c.get(f"http://127.0.0.1:{s['port']}/v1/models", headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
            data = r.json().get("data") or []
        else:
            data = fetch()
        ids = sorted({str(m.get("id") or "") for m in data if isinstance(m, dict)} - {""})
        _models_cache["ids"] = [i for i in ids if not any(k in i.lower() for k in _SKIP_MODELS)]
        # Track which credential/provider owns each model: Antigravity exposes Claude & GPT models
        # under its own account limits; without this mapping they were grouped with separate Claude/ChatGPT
        # subscriptions and their quota/account switching was wrong.
        owners: dict[str, str] = {}
        for m in data:
            if isinstance(m, dict):
                mid = str(m.get("id") or "")
                owner = str(m.get("owned_by") or "")
                if mid and owner:
                    owners[mid] = owner
        _models_cache["owners"] = owners
    except Exception:  # noqa: BLE001 - proxy is down: menu should still open
        pass
    return list(_models_cache["ids"])


def model_owners() -> dict[str, str]:
    """model ID -> credential/provider that owns it (e.g. "antigravity", "claude"). Populated by live_models().
    Antigravity accounts serve Claude & GPT models under Antigravity's own quota; this mapping lets model_group()
    attribute them correctly instead of mixing them with separate Claude/ChatGPT subscriptions."""
    return dict(_models_cache.get("owners") or {})


# UI groups for proxy models: (prefix, group ID, label). Also used as an ID hide pattern:
# hiding "cliproxy:kimi-*" hides all Kimi models.
# NOTE: Antigravity accounts also serve Claude & GPT models under Antigravity's own limits.
# model_group() uses the credential owner from /v1/models to attribute those correctly;
# prefix-based grouping is the fallback when no owner info is available.
GROUPS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("claude-",), "claude", "Claude (abonelik)"),
    (("gpt-", "codex", "o3", "o4"), "codex", "ChatGPT (abonelik)"),
    (("kimi-",), "kimi", "Kimi"),
    (("gemini-",), "antigravity", "Gemini (Antigravity)"),
    (("grok-",), "xai", "xAI Grok"),
)

# Credential owner values (from /v1/models owned_by) that map to the antigravity group.
# When an Antigravity account serves a Claude or GPT model, the proxy reports owned_by as one of these.
_ANTIGRAVITY_OWNERS = ("antigravity", "google", "google-antigravity")


def model_group(model: str) -> tuple[str, str, str]:
    """(group ID, label, hide pattern) - e.g. kimi-k3 -> ("kimi", "Kimi", "cliproxy:kimi-*").

    Antigravity accounts expose Claude & GPT models under Antigravity's own quota and limits (separate from
    standalone Claude/ChatGPT subscriptions). The owned_by field from /v1/models identifies the credential
    provider; when it indicates Antigravity, the model is grouped under "antigravity" regardless of its name
    prefix. This prevents incorrect quota tracking and account switching."""
    owners = _models_cache.get("owners") or {}
    owner = owners.get(model, "").lower()
    if owner in _ANTIGRAVITY_OWNERS:
        return "antigravity", "Antigravity", "cliproxy:antigravity-*"
    low = model.lower()
    for prefixes, gid, label in GROUPS:
        if low.startswith(prefixes):
            return gid, label, f"cliproxy:{prefixes[0]}*"
    return "other", "Diğer (proxy)", ""


def tidy_models(ids: list[str], keep: int = 2) -> list[str]:
    """Keep menu uncluttered: dated fixed versions (…-20250514) and older generations are dropped, keeping the newest
    `keep` versions per family. Family = name stripped of version numbers (claude-opus-#, kimi-k#-code, gpt-#-terra)."""
    import re as _re

    def nums(m: str) -> tuple[int, ...]:
        return tuple(int(x) for x in _re.findall(r"\d+", m) if len(x) < 4)

    fresh = [m for m in ids if not _re.search(r"-\d{8}$", m)]
    top: dict[str, int] = {}
    for m in fresh:  # newest major version per vendor (claude 5, kimi 3); names like gpt-oss-120b are not counted
        n = nums(m)
        if n and n[0] < 50:
            v = m.split("-", 1)[0]
            top[v] = max(top.get(v, 0), n[0])
    fams: dict[str, list[str]] = {}
    for m in fresh:
        n = nums(m)
        v = m.split("-", 1)[0]
        if n and n[0] < 50 and n[0] < top.get(v, 0) - 1:
            continue  # two generations old (claude-3-7-sonnet)
        fams.setdefault(_re.sub(r"\d+(?:[.-]\d+)*", "#", m), []).append(m)
    out: list[str] = []
    for members in fams.values():
        out += sorted(members, key=nums, reverse=True)[:keep]
    return sorted(out)


def _req(method: str, path: str) -> dict[str, Any]:
    s = settings()
    if not s["key"]:
        raise RuntimeError("CLIProxyAPI yönetim anahtarı yok (~/.alice/cliproxy/keys.txt)")
    url = f"http://127.0.0.1:{s['port']}{path}"
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.request(method, url, headers={"Authorization": f"Bearer {s['key']}"})
    if r.status_code >= 400:
        raise RuntimeError(f"proxy {r.status_code}: {r.text[:160]}")
    try:
        return r.json()
    except ValueError:
        return {}


# ------------------------------------------------------------------ startup
_start_lock = threading.Lock()
_last_start = 0.0
START_RETRY_SECONDS = 60.0


def running() -> bool:
    """If the management API responds, the proxy is running."""
    try:
        _req("GET", "/v8/management/credentials")
        return True
    except Exception:  # noqa: BLE001 - closed/unreachable
        return False


def ensure_affinity(path: Any) -> bool:
    """Add routing.session-affinity to config.yaml of older installations: keeps the same chat on the same account.
    When accounts are distributed round-robin, each request goes to a different account (different cache), and
    Gemini cache hit rate stayed at 40%. Left untouched if user explicitly set false. True if modified."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if "session-affinity" in text:
        return False
    m = re.search(r"(?m)^routing:[ \t]*\n", text)
    if m:
        new = text[:m.end()] + "  session-affinity: true\n" + text[m.end():]
    else:
        new = text.rstrip("\n") + "\nrouting:\n  session-affinity: true\n"
    try:
        path.write_text(new, encoding="utf-8")
    except OSError:
        return False
    return True


def ensure_routing_retry(path: Any) -> bool:
    """Ensure routing.retry and session-affinity-rebind-on: any are configured in config.yaml.
    When a credential hits quota or rate limits, CLIProxyAPI will automatically try other credentials
    in the pool (e.g. across multiple Grok or Antigravity accounts)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    changed = False
    if "session-affinity-rebind-on:" not in text and "routing:" in text:
        m = re.search(r"(?m)^([ \t]*session-affinity:[^\n]*\n)", text)
        if m:
            text = text[:m.end()] + "  session-affinity-rebind-on: \"any\"\n" + text[m.end():]
            changed = True
    if "request-retry:" not in text and "routing:" in text:
        retry_block = (
            "  retry:\n"
            "    request-retry: 8\n"
            "    max-retry-credentials: 0\n"
            "    max-retry-interval: 10\n"
        )
        m = re.search(r"(?m)^routing:[ \t]*\n", text)
        if m:
            text = text[:m.end()] + retry_block + text[m.end():]
            changed = True
    if changed:
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            return False
    return changed


def start(wait_seconds: float = 8.0) -> dict[str, Any]:
    """If proxy is down, start it in the background (independent of ALICE) and wait until endpoint responds."""
    global _last_start
    s = settings()
    if not s["installed"]:
        return {"ok": False, "error": "CLIProxyAPI kurulu değil"}
    if running():
        return {"ok": True, "already": True}
    with _start_lock:
        if running():
            return {"ok": True, "already": True}
        now = time.monotonic()
        if now - _last_start < START_RETRY_SECONDS and _last_start:
            return {"ok": False, "error": "az önce denendi; biraz sonra yeniden denenecek"}
        _last_start = now
        exe = s["dir"] / binary_name()
        if not exe.exists():
            return {"ok": False, "error": f"{binary_name()} bulunamadı"}
        ensure_affinity(s["dir"] / "config.yaml")
        ensure_routing_retry(s["dir"] / "config.yaml")
        try:
            log = open(s["dir"] / "server.out.log", "ab")  # noqa: SIM115 - child process will write
        except OSError:
            log = subprocess.DEVNULL
        kwargs: dict[str, Any] = {"cwd": str(s["dir"]), "stdout": log, "stderr": subprocess.STDOUT,
                                  "stdin": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW: keep running even if ALICE exits,
            # do not open a console window.
            kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
        try:
            subprocess.Popen([str(exe), "-config", str(s["dir"] / "config.yaml")], **kwargs)
        except OSError as e:
            return {"ok": False, "error": f"başlatılamadı: {e}"}
        finally:
            if log is not subprocess.DEVNULL:
                log.close()
        end = time.time() + wait_seconds
        while time.time() < end:
            if running():
                return {"ok": True, "started": True}
            time.sleep(0.3)
    return {"ok": False, "error": "proxy başladı ama cevap vermedi (server.out.log)"}


def ensure_started() -> dict[str, Any]:
    """Start if installed and down; silently skip if not installed."""
    if not settings()["installed"]:
        return {"ok": True, "skipped": "kurulu değil"}
    if running():
        return {"ok": True, "already": True}
    return start()


def start_in_background(install: bool = False, version: str = "") -> None:
    """Try starting the proxy (installing first if needed) without blocking ALICE startup."""

    def run() -> None:
        if install:
            try:
                from alice.app import cliproxy_setup

                cliproxy_setup.ensure_installed(version)
            except Exception:  # noqa: BLE001 - continue without proxy if download fails
                pass
        ensure_started()

    threading.Thread(target=run, daemon=True, name="alice-cliproxy").start()


def _pct(value: Any) -> int | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f * 100) if f <= 1 else round(f)


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# (signal prefix, label). Claude: 5-hour and weekly window; ChatGPT (Codex): primary and secondary window.
_WINDOWS = (("Anthropic-Ratelimit-Unified-5h", "5 saat"), ("Anthropic-Ratelimit-Unified-7d", "7 gün"),
            ("X-Codex-Primary", "kısa dönem"), ("X-Codex-Secondary", "hafta"))


def _window_label(sig: dict[str, Any], prefix: str, label: str) -> str:
    mins = _num(sig.get(f"{prefix}-Window-Minutes"))
    if not mins:
        return label
    return f"{round(mins / 1440)} gün" if mins >= 1440 else f"{round(mins / 60)} saat" if mins >= 60 else f"{round(mins)} dk"


def _iso_ts(value: Any) -> float:
    """ISO timestamp (with Go's 7-9 digit fractional seconds) -> unix; 0 if unreadable."""
    if not value:
        return 0.0
    from datetime import datetime

    text = re.sub(r"(\.\d{6})\d+", lambda m: m.group(1), str(value).strip()).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _quota_summary(quota: Any, now: float | None = None) -> dict[str, Any]:
    """Account quota windows: {"windows": [{label, percent, reset, stale}], label, percent, reset}. Top-level
    label/percent/reset is the most utilized window (the bar shows it). Previously only the first window (5h) was shown:
    an account at 91% weekly appeared as "13%". Signals remain from account's last request; a window past its reset time
    is now empty (0%, stale): a 5-hour window filled yesterday was still being shown as "100%" today."""
    sig = (quota or {}).get("signals") if isinstance(quota, dict) else None
    if not isinstance(sig, dict):
        return {}
    now = time.time() if now is None else now
    observed = _iso_ts((quota or {}).get("observed_at"))
    windows: list[dict[str, Any]] = []
    for prefix, label in _WINDOWS:
        pct = _pct(sig.get(f"{prefix}-Utilization")) if f"{prefix}-Utilization" in sig else _pct(sig.get(f"{prefix}-Used-Percent"))
        if pct is None:
            continue
        reset = _num(sig.get(f"{prefix}-Reset") or sig.get(f"{prefix}-Reset-At")) or 0
        after = _num(sig.get(f"{prefix}-Reset-After-Seconds"))
        if not reset and after is not None and observed:
            reset = observed + after
        stale = bool(reset and reset <= now)
        windows.append({"label": _window_label(sig, prefix, label), "percent": 0 if stale else pct,
                        "reset": 0 if stale else int(reset), "stale": stale})
    if not windows:
        return {}
    top = max(windows, key=lambda w: w["percent"])
    return {"label": top["label"], "percent": top["percent"], "reset": top["reset"], "windows": windows,
            "observed": int(observed or 0)}


def _duration(seconds: float) -> str:
    if seconds >= 86400:
        return f"{round(seconds / 86400)} gün"
    if seconds >= 3600:
        return f"{round(seconds / 3600)} sa"
    return f"{max(1, round(seconds / 60))} dk"


def _cooldown_summary(cooldowns: Any, now: float | None = None) -> str:
    """If any model is waiting, how much time is left ("3 gün", "2 sa", "15 dk"), otherwise empty. Proxy reports time
    via `remaining_seconds` and `retry_at`; previously these were not read, and card displayed "beklemede · beklemede"."""
    if not cooldowns:
        return ""
    items = cooldowns if isinstance(cooldowns, list) else [cooldowns]
    now = time.time() if now is None else now
    best = 0.0
    unknown = False  # unreadable duration record: proxy says waiting but duration is unknown
    for it in items:
        if not isinstance(it, dict):
            continue
        left = _num(it.get("remaining_seconds"))
        if left is None:
            until = it.get("retry_at") or it.get("until") or it.get("until_ts") or it.get("expires_at") or it.get("reset")
            ts = _num(until) or _iso_ts(until)
            left = ts - now if ts else None
        if left is None:
            unknown = True
        elif left > 0:
            best = max(best, left)
    # Expired records are not considered blockers: if proxy left without clearing, account stayed "busy" forever.
    return _duration(best) if best > 0 else "kısa süre" if unknown else ""


_SIGN_IN_CODES = ("invalid_grant", "refresh_failed", "unauthorized", "invalid_token")


def _account_error(f: dict[str, Any]) -> tuple[bool, str]:
    """(whether re-login is required, short error). Proxy reports an account with dead refresh token as
    `status: error, unavailable: true`, but `requires_sign_in` often remains false: card was showing them as "ready"."""
    err = f.get("refresh_error") if isinstance(f.get("refresh_error"), dict) else {}
    code = str(err.get("code") or "").lower()
    msg = str(err.get("message") or f.get("status_message") or "")
    if bool(f.get("requires_sign_in")) or code in _SIGN_IN_CODES:
        if "restricted" in msg.lower():
            return True, "hesap kısıtlanmış (sağlayıcı girişi reddediyor)"
        return True, "giriş süresi dolmuş"
    if str(f.get("status") or "").lower() == "error" or f.get("unavailable"):
        if f.get("quota_blocked") or _cooldown_summary(f.get("cooldowns")):
            return False, ""  # quota: displayed separately
        return False, " ".join(msg.split())[:120] or "hata"
    return False, ""


def _provider_of(name: str, provider: str) -> str:
    provider = str(provider or "").strip().lower()
    if provider:
        return provider
    for prefix, key in FILE_PREFIXES:
        if name.startswith(prefix):
            return key
    return ""


def _account(f: dict[str, Any]) -> dict[str, Any]:
    name = str(f.get("id") or f.get("name") or "")
    provider = _provider_of(name, str(f.get("provider") or ""))
    sign_in, error = _account_error(f)
    cooldown = _cooldown_summary(f.get("cooldowns"))
    return {
        "name": name,
        "email": str(f.get("email") or f.get("account") or f.get("label") or name),
        "provider": provider,
        "provider_name": PROVIDER_NAMES.get(provider, provider or "hesap"),
        "disabled": bool(f.get("disabled")),
        "status": str(f.get("status") or ""),
        "status_message": str(f.get("status_message") or ""),
        "needs_sign_in": sign_in,
        "error": error,
        "expires_at": str(f.get("access_token_expires_at") or ""),
        "quota_blocked": bool(f.get("quota_blocked")),
        "cooldown": cooldown,
        "quota": _quota_summary(f.get("quota")),
        "failed": int(f.get("failed") or 0),
        # Not raw cooldowns list, but currently active cooldown: expired records do not mark the account as busy.
        "ready": not (sign_in or error or f.get("disabled") or f.get("quota_blocked") or cooldown),
    }


READY_TTL = 60.0  # s
_ready_cache: dict[str, Any] = {"at": 0.0, "groups": {}}


def groups_ready(fetch: Any = None) -> dict[str, bool]:
    """Service (claude, codex, antigravity...) -> whether it has at least one usable account. Services without accounts
    are not in the dict. Router uses this to skip services with all accounts full/logged out: instead of trying that
    service first on every request and waiting for an error, it moves straight to next. Returns empty if proxy unreachable (none skipped)."""
    now = time.monotonic()
    if fetch is None and _ready_cache["at"] and now - _ready_cache["at"] < READY_TTL:
        return dict(_ready_cache["groups"])
    out: dict[str, bool] = {}
    try:
        data = fetch() if fetch is not None else _req("GET", "/v8/management/credentials")
        files = data.get("files") if isinstance(data, dict) else None
        for f in files or []:
            if isinstance(f, dict):
                acc = _account(f)
                if acc["provider"]:
                    out[acc["provider"]] = out.get(acc["provider"], False) or acc["ready"]
    except Exception:  # noqa: BLE001 - proxy is down: unknown
        out = {}
    if fetch is None:
        _ready_cache.update(at=now, groups=out)
    return dict(out)


def status() -> dict[str, Any]:
    """Everything the card needs: whether installed, running, accounts, and services that can be added."""
    s = settings()
    out: dict[str, Any] = {
        "installed": bool(s["installed"]),
        "running": False,
        "dir": str(s["dir"]),
        "port": s["port"],
        "accounts": [],
        "providers": [{"key": k, "name": n} for k, n in PROVIDERS],
        "error": "",
    }
    if not s["installed"]:
        out["error"] = "CLIProxyAPI kurulu değil"
        return out
    try:
        data = _req("GET", "/v8/management/credentials")
    except Exception as e:  # noqa: BLE001 - if down try starting it, if still fails report "not running"
        started = start()
        if not started.get("ok"):
            out["error"] = str(started.get("error") or e)[:200]
            return out
        try:
            data = _req("GET", "/v8/management/credentials")
        except Exception as e2:  # noqa: BLE001
            out["error"] = str(e2)[:200]
            return out
    out["running"] = True
    files = data.get("files") if isinstance(data, dict) else None
    out["accounts"] = [_account(f) for f in (files or []) if isinstance(f, dict)]
    out["accounts"].sort(key=lambda a: (a["provider"], a["email"]))
    return out


def login_url(provider: str) -> dict[str, Any]:
    """Initiate OAuth login link for a service: {ok, url, state}. User clicks the link and signs in with their account."""
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDER_NAMES and provider not in ("meta", "devin"):
        return {"ok": False, "error": "bilinmeyen servis"}
    try:
        # is_webui=1: proxy listens to localhost:54545/1455 returned by the browser and
        # redirects to management callback. Without this flag, browser got stuck on "connection refused"
        # and account was never added.
        d = _req("GET", f"/v8/management/oauth/auth-url?provider={quote(provider)}&is_webui=1")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    url = str(d.get("url") or "")
    if not url:
        return {"ok": False, "error": "giriş linki alınamadı"}
    return {"ok": True, "url": url, "state": str(d.get("state") or ""), "provider": provider}


def login_state(state: str) -> dict[str, Any]:
    """Whether sign-in is complete: if status is "ok", account is added; if "wait", it is ongoing."""
    state = str(state or "").strip()
    if not state:
        return {"ok": False, "error": "eksik state"}
    try:
        d = _req("GET", f"/v8/management/oauth/status?state={quote(state)}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, "status": str(d.get("status") or ""), "error": str(d.get("error") or "")}


def cancel_login(state: str) -> dict[str, Any]:
    """Cancel an abandoned sign-in."""
    state = str(state or "").strip()
    if not state:
        return {"ok": False, "error": "eksik state"}
    try:
        _req("DELETE", f"/v8/management/oauth/session?state={quote(state)}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True}


def remove(name: str) -> dict[str, Any]:
    """Remove an account (auth file). Name must be the file name: claude-....json."""
    name = str(name or "").strip()
    if not name.endswith(".json") or "/" in name or "\\" in name or ".." in name:
        return {"ok": False, "error": "geçersiz hesap adı"}
    try:
        _req("DELETE", f"/v8/management/credentials?name={quote(name)}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True}
