"""Checks for the new `alice doctor` features: each returns a row of (name, status, detail).

Status: "ok" working | "warn" working but missing/limited | "off" disabled/not installed (not an error) | "fail" broken.
Checks that make network requests run with a short timeout; if one fails, the others still run.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from alice.i18n import _


@dataclass
class Check:
    name: str
    status: str  # ok | warn | off | fail
    detail: str
    fix: str = ""


def _safe(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - if a check crashes, record it as "fail" so the others keep running
        return Check(name, "fail", f"{type(e).__name__}: {str(e)[:160]}")


# ------------------------------------------------------------------------------------------- checks
def _connect_calendar() -> str:
    return _("System → Setup → Connect calendar (command: alice connect google_calendar)")


def check_grounding(cfg, network: bool = True) -> Check:
    name = _("Google search")
    if not cfg.agent.get("search_grounding", True):
        return Check(name, "off", _("off ([agent] search_grounding = false)"))
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return Check(name, "off", _("no GEMINI_API_KEY: web_search uses DuckDuckGo/Bing"), _("add GEMINI_API_KEY"))
    if not network:
        return Check(name, "ok", _("key present (not tested)"))
    from alice.tools import web

    t0 = time.monotonic()
    try:
        res = web.gemini_search("today's date", key, str(cfg.agent.get("grounding_model", "gemini-flash-latest")),
                                timeout=20)
    except ValueError as e:
        return Check(name, "warn", _("not working ({error}); falling back to DuckDuckGo/Bing", error=str(e)[:120]),
                     _("grounding may be off on a free key; that's fine, the classic search works"))
    finally:
        web._grounding["down_until"] = 0.0  # the doctor's probe must not lock real usage for an hour
    return Check(name, "ok", _("working ({secs} s, {n} sources)", secs=f"{time.monotonic() - t0:.1f}",
                                n=len(res["sources"])))


def check_embedding(cfg, network: bool = True) -> Check:
    name = _("Semantic search (memory)")
    from alice.memvec import Embedder

    o = dict(cfg.data.get("memory") or {})
    emb = Embedder.from_config(cfg, o)
    if emb is None:
        return Check(name, "off", _("no embeddings (provider: {provider}, no key or off); memory uses keyword search",
                                    provider=o.get("embed_provider", "gemini")))
    if not network:
        return Check(name, "ok", _("{model} (not tested)", model=emb.model))
    t0 = time.monotonic()
    try:
        vec = emb.embed(["deneme"])[0]
    except Exception as e:  # noqa: BLE001
        return Check(name, "warn", _("{model} not working: {error}; falling back to keyword search", model=emb.model,
                                      error=str(e)[:120]))
    return Check(name, "ok", _("{model}, {dims} dimensions, {secs} s", model=emb.model, dims=len(vec),
                                secs=f"{time.monotonic() - t0:.1f}"))


def check_memory(cfg) -> Check:
    name = _("Memory")
    from alice.config import alice_home

    if not (cfg.data.get("memory") or {}).get("enabled", True):
        return Check(name, "off", _("off ([memory] enabled = false)"))
    if not (alice_home() / "memory.db").is_file():
        return Check(name, "ok", _("empty so far (it fills up with the first conversations)"))
    from alice.memory import shared_memory

    mem = shared_memory(cfg)
    if mem is None:
        return Check(name, "off", _("off"))
    with mem._lock:
        vecs = mem.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    sums = len(mem.summaries(1000))
    notes = sum(len(b.scan()) for b in mem.all_books())
    return Check(name, "ok", _("{notes} notes, {sums} conversation summaries, {followups} open follow-ups, {vecs} vectors",
                               notes=notes, sums=sums, followups=len(mem.followups()), vecs=vecs))


def check_projects(cfg) -> Check:
    name = _("Projects")
    from alice import projects

    if not projects.enabled(cfg):
        return Check(name, "off", _("off ([projects] enabled = false)"))
    scanned, found = projects.load()
    if not scanned:
        return Check(name, "warn", _("not scanned yet (scanned in the background on first start)"),
                     _("start a conversation with alice"))
    age = (time.time() - scanned) / 3600
    top = ", ".join(p.name for p in found[:4])
    return Check(name, "ok" if found else "warn", _("{n} projects (scanned {hours} hours ago)", n=len(found),
                                                       hours=f"{age:.0f}")
                 + (f": {top}" if top else ""), "" if found else _("add your project folder to [projects] paths"))


def check_calendar(cfg, network: bool = True) -> Check:
    name = _("Google Calendar")
    from alice.connect import runner
    from alice.connect.manifest import discover

    m = discover(cfg)[0].get("google_calendar")
    if m is None:
        return Check(name, "off", _("connection definition is off"))
    ok, why = runner.ready(m)
    if ok and network and "upcoming" in m.actions:
        # Looked "Bağlı" (connected) but 404'd on every request ({calendarId} unfilled): read it once for real.
        from datetime import datetime, timedelta

        now = datetime.now().astimezone()
        try:
            runner.call(m, m.actions["upcoming"], {"timeMin": now.isoformat(),
                                                   "timeMax": (now + timedelta(days=1)).isoformat(), "maxResults": 1},
                        cache=False)
        except Exception as e:  # noqa: BLE001
            return Check(name, "fail", _("connected but can't be read: {error}", error=str(e)[:120]), _connect_calendar())
    if ok:
        return Check(name, "ok", _("connected"))
    if "yeniden giriş" in why:
        # Was connected, but Google rejects the sign-in: not "off" (disabled), but something the user must fix.
        return Check(name, "fail", _("the Google sign-in is invalid; ALICE can't refresh the token itself"),
                     _connect_calendar() + _(". If it happens every week, set the OAuth app to 'In production' in "
                                            "Google Cloud"))
    fix = _("System → Setup → Google (command: alice setup google)") if "GOOGLE_CLIENT_ID" in why or \
        not os.environ.get("GOOGLE_CLIENT_ID") else _connect_calendar()
    return Check(name, "off", why, fix)


def check_gmail(cfg, network: bool = True) -> Check:
    name = "Gmail"
    from alice.connect import store
    from alice.tools import gmail

    accs = gmail.accounts()
    if not accs:
        return Check(name, "off", _("no connected account"), _("System → Setup → Add Gmail (command: alice setup google)"))
    from alice.connect import oauth

    parts, bad = [], []
    for acc in accs:
        email = store.get(gmail.PREFIX + acc).get("email", "")
        if oauth.needs_sign_in(gmail.PREFIX + acc):
            bad.append(_("{account}: sign-in invalid, sign in again", account=acc))
            continue
        if network:
            try:
                gmail.token(acc)
            except Exception as e:  # noqa: BLE001
                bad.append(f"{acc}: {str(e)[:80]}")
                continue
        parts.append(f"{acc}" + (f" ({email})" if email else ""))
    if bad:
        return Check(name, "warn", "; ".join(parts + bad),
                     _("connect the broken account again: System → Setup → Add Gmail (same name)"))
    return Check(name, "ok", ", ".join(parts))


def check_sandbox(cfg) -> Check:
    name = _("Command sandbox")
    from alice import sandbox

    ctx = type("C", (), {"config": cfg})()
    m = sandbox.mode(ctx)
    if m == "off":
        return Check(name, "off", _("off ([agent] sandbox = off)"))
    backend, problem = sandbox.backend()
    if problem:
        return Check(name, "warn", _("{mode} mode but it can't be set up: {problem}", mode=m, problem=problem)
                     + (_("; in a turn that read outside content, commands ask for approval") if m == "auto"
                        else _("; commands fail")),
                     problem.split(": ", 1)[-1] if "apt" in problem else "")
    return Check(name, "ok", _("{mode} mode, backend: {backend}", mode=m, backend=backend))


def check_greeting(cfg) -> Check:
    name = _("Welcome back")
    from alice import presence

    s = presence.settings(cfg)
    if not s.get("enabled", True):
        return Check(name, "off", _("off ([greeting] enabled = false)"))
    idle = presence.idle_seconds()
    if idle is None:
        return Check(name, "warn", _("idle time can't be measured (Linux needs xprintidle)"),
                     "sudo apt install xprintidle")
    return Check(name, "ok", _("after {minutes} min away; idle for {secs} s now (while the core runs)",
                                minutes=s["away_minutes"], secs=f"{idle:.0f}"))


def check_rules(cfg, cwd=None) -> Check:
    name = _("Rules, hooks, skills")
    from alice.hooks import Hooks
    from alice.permissions import Permissions
    from alice.skills import load_agents, load_skills

    ctx = type("C", (), {"config": cfg, "cwd": cwd})()
    rules = Permissions("auto").rules_for(ctx)
    hooks = Hooks.from_config(cfg, cwd)
    return Check(name, "ok", _("permission rules {rules} (allow {allow}, deny {deny}), hooks {hooks}, skills {skills}, "
                               "subagents {agents}", rules=sum(len(v) for v in rules.values()),
                               allow=len(rules["allow"]), deny=len(rules["deny"]), hooks=len(hooks.hooks),
                               skills=len(load_skills(cwd)), agents=len(load_agents(cwd))))


def check_update_health(cfg) -> Check:
    name = _("Update health")
    from alice import updater

    st = updater.read_state()
    rb = st.get("rollback") if isinstance(st.get("rollback"), dict) else None
    bad = st.get("bad") or []
    if rb and time.time() - float(rb.get("ts") or 0) < 7 * 86400:
        return Check(name, "warn", _("the last update was rolled back ({old} → {new}): {reason}",
                                     old=str(rb.get("from"))[:7], new=str(rb.get("to"))[:7], reason=rb.get("reason")),
                     _("it updates by itself once a fix commit arrives"))
    if st.get("pending_health"):
        return Check(name, "ok", _("trying a new version (counted as healthy after 90 s up)"))
    return Check(name, "ok", _("no problems") + (_(" ({n} broken versions skipped)", n=len(bad)) if bad else ""))


def check_ocr(cfg) -> Check:
    name = _("Screen reading (OCR)")
    from alice.reflex import perceive

    ok, why = perceive.ocr_available()
    if ok:
        return Check(name, "ok", _("Windows OCR ready: reflex reads the text on screen too"))
    from alice.deps import install_command

    return Check(name, "off", _("{why}; reflex works only with the accessibility tree", why=why),
                 _("ALICE installs missing packages itself on start; right now: {command}",
                   command=install_command([])) if "paket" in why else "")


def run_all(cfg, cwd=None, network: bool = True) -> list[Check]:
    return [
        _safe(_("Google search"), lambda: check_grounding(cfg, network)),
        _safe(_("Semantic search (memory)"), lambda: check_embedding(cfg, network)),
        _safe(_("Memory"), lambda: check_memory(cfg)),
        _safe(_("Projects"), lambda: check_projects(cfg)),
        _safe(_("Google Calendar"), lambda: check_calendar(cfg, network)),
        _safe("Gmail", lambda: check_gmail(cfg, network)),
        _safe(_("Command sandbox"), lambda: check_sandbox(cfg)),
        _safe(_("Welcome back"), lambda: check_greeting(cfg)),
        _safe(_("Rules, hooks, skills"), lambda: check_rules(cfg, cwd)),
        _safe(_("Screen reading (OCR)"), lambda: check_ocr(cfg)),
        _safe(_("Update health"), lambda: check_update_health(cfg)),
    ]


def test_ref(router, name: str, pc: Any, explicit: str = "") -> str:
    """Model for testing a provider: the one written in the role, else the best working model for it in the measurement
    cache, else the first of its fixed list. With roles on "auto", providers showed as "atlandı" (skipped)."""
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


PROBE_TIMEOUT = 30.0  # sec: in "Try all", a provider waits at most this long (normally 180 sec)
PROBE_TOKENS = 512  # thinking models (GLM Flash) think first; with 50 tokens they were cut off before answering


# For a credit-billed provider (Vercel), the model that looks cheapest to test with; pattern order matters.
CHEAP_HINTS = ("flash-lite", "nano", "haiku", "mini", "small", "flash", "8b")


def test_refs(router, name: str, pc: Any, explicit: str = "", limit: int = 3) -> list[str]:
    """Candidate models for a provider (in order, at most limit): the one written in the role, the working ones
    from the last measurement, other measured ones, else the provider's live catalog. A never-measured provider
    (SambaNova) used to look "atlandı" (skipped); when one model was busy (Z.ai), its sibling went red untried."""
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
        except Exception:  # noqa: BLE001 - if the catalog can't be fetched, try with what we have
            ids = []
        ids = [i for i in ids if i and not any(x in i.lower() for x in ranking.EXCLUDE)]
        if not getattr(pc, "auto", True):  # credit-billed: the one that looks cheapest
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
    """Short readable text from a provider error: the "message" inside instead of raw JSON; known cases in plain words."""
    import re

    kind = ""
    m = re.match(r"\[(\w+)\]\s*", err)
    if m:
        kind, err = m.group(1), err[m.end():]
    low = err.lower()
    if "payment_method_required" in low or "payment method is required" in low:
        return _("wants a payment method: the provider requires a card on the account even for free use")
    if "insufficient" in low and ("credit" in low or "balance" in low or "funds" in low):
        return _("out of credit or has none")
    msg = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', err)
    text = (msg.group(1) if msg else err).replace("\\n", " ").strip()
    text = " ".join(text.split())
    prefix = {"auth": _("key rejected"), "quota": _("quota full or payment needed"), "not_found": _("no such model"),
              "rate_limit": _("rate limited/busy"), "server": _("provider error"),
              "too_large": _("request too large")}.get(kind, "")
    out = f"{prefix}: {text}" if prefix and text else (prefix or text)
    return out if len(out) <= limit else out[:limit - 1] + "…"


def probe_providers(cfg: Any, emit: Callable[[str], None] = lambda s: None,
                    timeout: float = PROBE_TIMEOUT, factory: Callable[[Any], Any] | None = None,
                    only: set[str] | None = None) -> list[dict[str, Any]]:
    """A short request to each provider: `alice doctor` and the app's Kurulum → Hepsini dene (Setup → Try all) share it.
    status: ok | warn (temporary: busy/rate limit) | fail | off (no key, not installed, chosen by hand).
    `only`: probe just these providers (the setup wizard tests the key that was just pasted)."""
    from alice.providers.chatgpt import make_client
    from alice.providers.router import AllProvidersFailed, Router

    router = Router(cfg, max_wait=0, provider_factory=factory or (lambda pc: make_client(pc, timeout=timeout)))
    explicit: dict[str, str] = {}
    for chain in cfg.roles.values():
        for ref in chain:
            p = ref.split(":", 1)[0]
            if p in router.providers_cfg:  # "auto" is not a provider
                explicit.setdefault(p, ref)
    rows = []
    for name, pc in router.providers_cfg.items():
        if only is not None and name not in only:
            continue
        key = "gerekmez" if not pc.needs_key else ("var" if pc.api_key else "yok")
        row = {"name": name, "key": key, "env": pc.api_key_env or "", "model": "", "status": "off", "detail": ""}
        rows.append(row)
        if not pc.enabled:
            row["detail"] = (_("no key ({env})", env=pc.api_key_env) if key == "yok"
                             else _("account ID missing") if "${" in pc.base_url else _("off"))
            continue
        credit = not getattr(pc, "auto", True)
        refs = test_refs(router, name, pc, explicit.get(name, ""), limit=1 if credit else 3)
        if not refs:
            row["detail"] = _("key present but the model list couldn't be fetched (the address or key may be wrong)")
            continue
        tried: list[str] = []
        for ref in refs:
            emit(_("trying {name} ({model})…", name=name, model=ref.split(":", 1)[1]))
            row["model"] = ref
            t0 = time.monotonic()
            try:
                res = router.chat(ref, [{"role": "user", "content": "Reply with just: ok"}], max_tokens=PROBE_TOKENS)
            except AllProvidersFailed as e:
                err = (e.errors[-1] if e.errors else _("error")).split(": ", 1)[-1]
                tried.append(f"{ref.split(':', 1)[1]}: {short_err(err, 70)}")
                if not pc.needs_key and ("unreachable" in err or "bağlanılamadı" in err):
                    row.update(status="off", detail=_("not running (a local model; fine if you didn't install it)"))
                    break
                if err.startswith("[auth]"):
                    row.update(status="fail", detail=short_err(err) + _(" · check it in Connections → Keys"))
                    break
                if "payment" in err.lower() and "required" in err.lower():
                    row.update(status="fail", detail=short_err(err))
                    break  # other models are on the same account too: no need to try them
                if err.startswith(("[rate_limit]", "[server]")):
                    row.update(status="warn", detail=_("busy or rate limited right now (provider side, temporary); "
                                                       "ALICE goes on with the next model"))
                elif "zaman aşımı" in err:
                    row.update(status="fail", detail=_("no answer in {secs} s; slow or stuck right now, ALICE goes on "
                                                       "with the next model for a while", secs=f"{timeout:.0f}"))
                else:
                    row.update(status="fail", detail=short_err(err))
                continue
            if not (res.text or "").strip():
                tried.append(_("{model}: empty answer", model=ref.split(":", 1)[1]))
                row.update(status="fail", detail=_("returned an empty answer"))
                continue
            row.update(status="ok", detail=_("{secs} s", secs=f"{time.monotonic() - t0:.1f}")
                       + (_(" · uses credit (very little)") if credit else ""))
            break
        if row["status"] != "ok" and len(tried) > 1:
            row["detail"] += _(" · tried: {items}", items="; ".join(tried))
        elif row["status"] == "ok" and tried:
            row["detail"] += _(" · the first one tried failed ({why})", why=tried[0][:60])
    return rows
