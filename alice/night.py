"""Night loop and dream mode: two sides of the same night job.

Night loop (self-improvement): overnight ALICE reads its own logs and finds repeated errors, unnecessary callouts
(the same sentence many times in a short time), false wakes, and its own tools that have broken. In the morning it
asks "I found these, shall I try to fix them?" It does not change code on its own: if you say yes, it makes the fix
with its usual tools (and approvals). Nothing gets merged without your approval.

Dream mode: replays the day from the start, works out tomorrow's tasks and looks for connections you cannot see:
"On the days after you play past 1 a.m., half of your flights end without a landing." A connection is only
mentioned when there is enough data.

The report goes to ~/.alice/night/<date>.md. The core runs it once between 03:00 and 07:00 and announces it at the
first suitable moment after 08:00 (silent in flight mode, announces later). Manually: alice night.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

NIGHT_HOURS = (3, 7)        # between these hours, once per night
MORNING_HOUR = 8            # from this hour on, announced at the first suitable moment
ERROR_REPEAT = 3            # the same error this many times in one day
NOISY_WINDOW = 600.0        # the same callout within 10 minutes...
NOISY_COUNT = 4             # ...repeated this many times or more counts as unnecessary
FALSE_WAKES = 5             # this many false wakes in one day
LATE_HOUR = 1               # playing after this hour (and before 6 a.m.) counts as "late"
MIN_DAYS = 4                # for a connection, at least this many days in each group


@dataclass
class Finding:
    kind: str        # error | noisy | wake | tool | crash
    title: str
    detail: str
    count: int
    suggestion: str


def state_path() -> Path:
    return alice_home() / "night-state.json"


def reports_dir() -> Path:
    return alice_home() / "night"


def _state() -> dict[str, Any]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(**kw: Any) -> None:
    """Atomic write: a write cut short (crash or shutdown) emptied the state, so the night loop ran again the same
    night and the morning report was spoken again. The temp file name is unique; on Windows, if the target is open
    in another process at that moment, os.replace is retried briefly."""
    s = {**_state(), **kw}
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(s, ensure_ascii=False))
        for attempt in range(10):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _pattern(text: str) -> str:
    """Ignores numbers and clock positions: "Arkanda düşman, saat 6, 1,2 km" -> "arkanda düşman, saat #"."""
    t = re.sub(r"\d+(?:[.,]\d+)?", "#", (text or "").lower())
    t = re.sub(r"#\s*(kilometre|metre|km|m|knot|kilo|feet|derece|°c)\b", "#", t)
    return " ".join(t.split())[:120]


# ------------------------------------------------------------------ findings
def repeated_errors(events: list[Any]) -> list[Finding]:
    groups: Counter[str] = Counter()
    sample: dict[str, Any] = {}
    for e in events:
        if e.kind != "error":  # a critical flight callout (fuel) is not an error
            continue
        key = _pattern(e.title)
        groups[key] += 1
        sample.setdefault(key, e)
    out = []
    for key, n in groups.most_common():
        if n < ERROR_REPEAT:
            break
        e = sample[key]
        out.append(Finding("error", e.title, (e.body or "")[:240], n,
                           "Hatanın kaynağını bulup düzelteyim ya da gereksizse kaydını kapatayım."))
    return out


def noisy_callouts(events: list[Any]) -> list[Finding]:
    """Same callout repeated in a short time: chatter that annoys the user (e.g. tonight's War Thunder spam)."""
    times: dict[str, list[float]] = defaultdict(list)
    text: dict[str, str] = {}
    for e in events:
        if e.kind != "flight" or not e.body or e.title != "Uçuş":
            continue
        key = _pattern(e.body)
        times[key].append(e.ts)
        text.setdefault(key, e.body)
    out = []
    for key, ts in times.items():
        ts.sort()
        worst, j = 0, 0
        for i in range(len(ts)):
            while ts[i] - ts[j] > NOISY_WINDOW:
                j += 1
            worst = max(worst, i - j + 1)
        if worst >= NOISY_COUNT:
            out.append(Finding("noisy", f"Çok tekrarlanan anons: \"{text[key][:70]}\"",
                               f"10 dakikada {worst} kez, gün boyunca {len(ts)} kez.", worst,
                               "Bu anonsun tekrar süresini uzatayım ya da sadece HUD'da göstereyim."))
    return sorted(out, key=lambda f: -f.count)


def false_wakes(lines: list[str], day: str) -> Finding | None:
    """voice.log: lines where "uyandı" (woke up) was logged but the spoken text does not contain Alice."""
    n, examples = 0, []
    for ln in lines:
        if not ln.startswith(day) or " uyandı " not in ln:
            continue
        said = ln.split("]:", 1)[-1].strip()
        if re.search(r"\b(alice|alis|alıs|elis|alisa)", said, re.I):
            continue
        n += 1
        if len(examples) < 3 and said:
            examples.append(f"\"{said[:40]}\"")
    if n < FALSE_WAKES:
        return None
    return Finding("wake", f"{n} yanlış uyanma", "Örnek: " + ", ".join(examples) if examples else "", n,
                   "Uyandırma eşiğini biraz yükselteyim ya da bu cümleleri ses modeline yanlış örnek olarak vereyim.")


def broken_tools() -> list[Finding]:
    try:
        from alice import selftools

        reg = selftools.load_registry()
    except Exception:  # noqa: BLE001
        return []
    return [Finding("tool", f"Kendi yazdığım araç kapandı: {n}", i.get("last_error", ""), int(i.get("errors", 0)),
                    "Aracı düzeltip testini yeniden kum havuzunda koşayım ya da kaldırayım.")
            for n, i in reg.items() if i.get("disabled") and not i.get("removed")]


def crashes(log_text: str) -> list[Finding]:
    """Last lines of the Python error tracebacks in core.log (the most frequently repeated ones)."""
    last = re.findall(r"(?m)^Traceback \(most recent call last\):\n(?:[ \t].*\n)+(\w[\w.]*(?:Error|Exception)[^\n]*)",
                      log_text)
    out = []
    for line, n in Counter(x[:160] for x in last).most_common(3):
        if n >= 2:
            out.append(Finding("crash", f"Tekrarlayan çökme: {line[:90]}", "", n,
                               "Hata yığınına bakıp kaynağını düzelteyim."))
    return out


def thread_deaths(log_text: str) -> list[Finding]:
    """Background thread died: even a single occurrence matters. On 2026-11-02 the audio thread crashed once,
    and ALICE heard nothing until it was restarted; the "at least 2 times" rule kept it out of the report."""
    hits = re.findall(r"(?m)^Exception in thread ([\w.-]+):\n(?:(?:Traceback|[ \t]).*\n)*?(\w[\w.]*(?:Error|Exception)"
                      r"[^\n]*)", log_text)
    out = []
    for (name, err), n in Counter((t, e[:140]) for t, e in hits).most_common(5):
        out.append(Finding("crash", f"Arka plan işi öldü: {name} ({err[:70]})", err, n,
                           "O özellik ALICE yeniden başlatılana kadar çalışmadı. Hata yığınına bakıp kaynağını "
                           "düzelteyim ve iş parçacığı bir daha kalıcı ölmesin."))
    return out


# Sentences where the user says a task went badly (chat and voice). Everyday profanity ("aq", "amk") and
# words like "takılmaca" (fooling around) are not counted: the user uses them in every message.
FRUSTRATION = re.compile(r"sa[çc]mala|mal gibi|malca|olmad[ıi](?![a-zçğıöşü])|yapamad[ıi]n|yapmad[ıi]n|bi t[üu]rl[üu]|"
                         r"tak[ıi]ld[ıi]|yanl[ıi][şs] yapt|ignore|duymad[ıi]n|cevap vermedi|berbat|💔", re.I)
STUCK_REPEAT = 3   # the same tool call this many times in one turn
STUCK_STEPS = 4    # the same step (e.g. "pressed enter") this many times in one screen-task result


def struggles(sessions: Path | None, since: float) -> list[Finding]:
    """Struggles from chat logs: repeating the same call again and again, pressing the same key and stalling in a
    screen task, turns the user cut off, and tasks the user called "yine saçmaladı" ("messed up again"). Each one
    comes with what was asked."""
    d = sessions or alice_home() / "sessions"
    out: list[Finding] = []
    try:
        files = [f for f in d.glob("*.json") if f.stat().st_mtime >= since]
    except OSError:
        return []
    for f in sorted(files):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        msgs = data.get("messages") if isinstance(data, dict) else data
        if not isinstance(msgs, list):
            continue
        ask, calls, prev_ask = "", Counter(), ""
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role, content = m.get("role"), m.get("content")
            text = content if isinstance(content, str) else " ".join(
                x.get("text", "") for x in content or [] if isinstance(x, dict))
            if role == "user" and not m.get("_nudge") and not m.get("_image") and not text.startswith("["):
                if FRUSTRATION.search(text) and prev_ask:
                    out.append(Finding("stuck", f"Kullanıcı memnun kalmadı: \"{text[:60]}\"",
                                       f"Önceki istek: \"{prev_ask[:120]}\"", 1,
                                       "O turdaki araç adımlarına bakıp nerede yanlış gittiğini bulayım ve genel bir "
                                       "düzeltme önereyim (sadece o işe özel yama değil)."))
                prev_ask = ask or text
                ask, calls = text, Counter()
            elif role == "assistant":
                for c in m.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    key = f"{fn.get('name')}({str(fn.get('arguments') or '')[:120]})"
                    calls[key] += 1
                    if calls[key] == STUCK_REPEAT:
                        out.append(Finding("stuck", f"Aynı adımı tekrarladı: {fn.get('name')}",
                                           f"İstek: \"{ask[:100]}\"; çağrı: {key[:140]}", STUCK_REPEAT,
                                           "Neden ilerleyemediğine bakıp bu durumda başka yol deneyecek genel bir kural "
                                           "ekleyeyim."))
            elif role == "tool":
                steps = Counter(s.strip() for s in re.findall(
                    r"\d+\. ((?:pressed|clicked|typed|right-clicked|double-clicked) .{1,40}?)(?=\s*(?:\(|\||\n|\d+\. |$))",
                    text))
                for step, n in steps.items():
                    if n >= STUCK_STEPS:
                        out.append(Finding("stuck", f"Ekran işinde aynı adım {n} kez: {step.strip()[:40]}",
                                           f"İstek: \"{ask[:100]}\"", n,
                                           "Ekranda beklenmedik bir pencere ya da hata olabilir; ekran işinin bu "
                                           "durumda durup ne gördüğünü söylemesini sağlayayım."))
                if "durduruldu (kullanıcı kesti)" in text:
                    out.append(Finding("stuck", "Kullanıcı işi yarıda kesti", f"İstek: \"{ask[:120]}\"", 1,
                                       "Kesilmeden önceki adımlara bakıp neyin yanlış gittiğini bulayım."))
    # merge the same finding if it was found more than once
    merged: dict[str, Finding] = {}
    for x in out:
        if x.title in merged:
            merged[x.title].count += x.count
        else:
            merged[x.title] = x
    return sorted(merged.values(), key=lambda x: -x.count)[:8]


# --------------------------------------------------------------------- dream
def _day_of(ts: float) -> str:
    """Hours after midnight count as the previous day (a match played at 2 a.m. counts as "last night")."""
    return (datetime.fromtimestamp(ts) - timedelta(hours=6)).strftime("%Y-%m-%d")


def insights(flights: list[dict[str, Any]]) -> list[str]:
    """Hidden connections. Almost all War Thunder flights end without a landing (death or respawn), so the
    "no-landing rate" tells nothing: a flight that goes down quickly is short. The measure is average flight time."""
    fs = [f for f in flights if f.get("start_wall") and f.get("duration_s")]
    if len(fs) < 20:
        return []

    def avg_min(group: list[dict[str, Any]]) -> float:
        return sum(float(f["duration_s"]) for f in group) / len(group) / 60

    out = []
    late_nights: set[str] = set()
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in fs:
        start = float(f["start_wall"])
        by_day[_day_of(start)].append(f)
        if LATE_HOUR <= datetime.fromtimestamp(start).hour < 6:
            late_nights.add(_day_of(start))
    after_late: list[dict[str, Any]] = []
    after_normal: list[dict[str, Any]] = []
    days_late = days_normal = 0
    for day, group in by_day.items():
        prev = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        if prev not in by_day:
            continue  # the previous day was never played: unknown whether you went to bed late
        if prev in late_nights:
            after_late += group
            days_late += 1
        else:
            after_normal += group
            days_normal += 1
    if days_late >= MIN_DAYS and days_normal >= MIN_DAYS:
        a, b = avg_min(after_late), avg_min(after_normal)
        if b >= a * 1.25:
            out.append(f"Gece {LATE_HOUR}'den sonra oynadığın günlerin ertesinde uçuşların ortalama {a:.1f} dakika "
                       f"sürüyor, diğer günlerde {b:.1f}: yorgunken daha çabuk düşüyorsun.")
        elif a >= b * 1.25:
            out.append(f"İlginç: geç oynadığın gecelerin ertesinde uçuşların daha uzun sürüyor ({a:.1f} dk, diğer "
                       f"günlerde {b:.1f}).")
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in fs:
        h = datetime.fromtimestamp(float(f["start_wall"])).hour
        buckets["gece 1-6" if 1 <= h < 6 else "akşam 18-1" if h >= 18 or h < 1 else "gündüz"].append(f)
    mins = {k: avg_min(v) for k, v in buckets.items() if len(v) >= 10}
    if len(mins) >= 2:
        hi, lo = max(mins, key=mins.__getitem__), min(mins, key=mins.__getitem__)
        if mins[hi] >= mins[lo] * 1.25:
            out.append(f"Uçuşların {lo} saatlerinde ortalama {mins[lo]:.1f} dakika sürüyor, {hi} saatlerinde "
                       f"{mins[hi]:.1f}.")
    return out


def tomorrow(mem: Any, now: float) -> list[str]:
    """Reminders and follow-ups due tomorrow (so what you ask first in the morning is ready)."""
    if mem is None:
        return []
    try:
        # Look ahead: follow-ups that count as "expired" at that moment are still valid today, so don't close them.
        due = mem.due_followups(now + 36 * 3600, prune=False)
    except Exception:  # noqa: BLE001
        return []
    return [f.text for f in due if (getattr(f, "due", None) or 0) > now][:5]


# ---------------------------------------------------------------------- flow
def analyze(store: Any, now: float, voice_log: Path | None = None, core_log: Path | None = None,
            sessions: Path | None = None) -> list[Finding]:
    since = now - 86400
    events = store.events(since=since, limit=5000)
    found = repeated_errors(events) + noisy_callouts(events) + struggles(sessions, since)
    vl = voice_log or alice_home() / "voice.log"
    try:
        lines = vl.read_text(encoding="utf-8", errors="replace").splitlines()[-20000:]
        for day in {datetime.fromtimestamp(since).strftime("%Y-%m-%d"), datetime.fromtimestamp(now).strftime("%Y-%m-%d")}:
            w = false_wakes(lines, day)
            if w is not None:
                found.append(w)
    except OSError:
        pass
    found += broken_tools()
    cl = core_log or alice_home() / "core.log"
    try:
        with cl.open("rb") as f:
            f.seek(max(0, cl.stat().st_size - 400_000))
            text = f.read().decode("utf-8", errors="replace")
            found += thread_deaths(text) + crashes(text)
    except OSError:
        pass
    return found


def run(store: Any, mem: Any = None, now: float | None = None, flights: list[dict[str, Any]] | None = None,
        **paths: Any) -> dict[str, Any]:
    now = time.time() if now is None else now
    found = analyze(store, now, **paths)
    if flights is None:
        from alice.sims.flight import load_history

        flights = load_history(limit=400)
    report = {"date": datetime.fromtimestamp(now).strftime("%Y-%m-%d"), "findings": [asdict(f) for f in found],
              "insights": insights(flights), "tomorrow": tomorrow(mem, now)}
    d = reports_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{report['date']}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    md = d / f"{report['date']}.md"
    md.write_text(render(report), encoding="utf-8")
    report["path"] = str(md)
    return report


def render(r: dict[str, Any]) -> str:
    lines = [f"# Gece raporu: {r['date']}", ""]
    if r["findings"]:
        lines += ["## Bulduklarım", ""]
        for i, f in enumerate(r["findings"], 1):
            lines.append(f"{i}. **{f['title']}** ({f['count']}). {f['detail']}".rstrip())
            lines.append(f"   Öneri: {f['suggestion']}")
        lines.append("")
    if r["insights"]:
        lines += ["## Bağlantılar", ""] + [f"- {x}" for x in r["insights"]] + [""]
    if r["tomorrow"]:
        lines += ["## Yarın", ""] + [f"- {x}" for x in r["tomorrow"]] + [""]
    if len(lines) == 2:
        lines.append("Her şey yolunda görünüyor.")
    return "\n".join(lines) + "\n"


def morning_text(r: dict[str, Any]) -> str:
    parts = []
    fs = r.get("findings") or []
    if fs:
        names = "; ".join(f["title"] for f in fs[:3])
        parts.append(f"Gece {len(fs)} şey buldum: {names}. Düzeltmeyi deneyeyim mi?")
    if r.get("insights"):
        parts.append(r["insights"][0])
    if r.get("tomorrow"):
        parts.append("Bugün: " + "; ".join(r["tomorrow"][:2]) + ".")
    return " ".join(parts)


def maybe_run(store: Any, mem: Any, now: float | None = None, **kw: Any) -> dict[str, Any] | None:
    """The core calls this every 10 minutes: between 03:00 and 07:00, once per night."""
    now = time.time() if now is None else now
    t = datetime.fromtimestamp(now)
    day = t.strftime("%Y-%m-%d")
    st = _state()
    if not NIGHT_HOURS[0] <= t.hour < NIGHT_HOURS[1] or st.get("ran") == day:
        return None
    tries = int(st.get("tries", 0)) + 1 if st.get("tried") == day else 1
    try:
        report = run(store, mem, now, **kw)
    except Exception:
        # a broken record must not lose the night: it is tried 3 times, then that night is dropped
        _save_state(tried=day, tries=tries, **({"ran": day} if tries >= 3 else {}))
        raise
    _save_state(ran=day)
    store.add_event("info", "Gece raporu hazır", report["path"], "info", "night")
    return report


def maybe_tell(send: Callable[[str, str], bool], now: float | None = None) -> bool:
    """After 08:00, say the report once. If notifications are muted (flight mode), retry later."""
    now = time.time() if now is None else now
    t = datetime.fromtimestamp(now)
    day = t.strftime("%Y-%m-%d")
    st = _state()
    if t.hour < MORNING_HOUR or st.get("ran") != day or st.get("told") == day:
        return False
    try:
        r = json.loads((reports_dir() / f"{day}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    text = morning_text(r)
    if not text:
        _save_state(told=day)
        return False
    if send("Gece raporu", text):
        _save_state(told=day)
        return True
    return False


def night_cmd(argv: list[str]) -> int:
    """`alice night`: run the night job now and write the report."""
    from alice.core.store import Store

    with Store() as st:
        try:
            from alice.config import load_config
            from alice.memory import shared_memory

            mem = shared_memory(load_config(), None)
        except Exception:  # noqa: BLE001
            mem = None
        r = run(st, mem)
    print(render(r))
    print(f"(rapor: {r['path']})")
    return 0
