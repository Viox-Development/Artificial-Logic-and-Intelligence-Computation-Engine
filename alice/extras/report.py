"""Weekly computer report: temperatures, memory, disks, top resource users, alerts."""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from alice.i18n import _, language
from alice.config import alice_home
from alice.core.store import Store

REPORT_PROMPT = ("Prepare my weekly computer report: call the report tool with kind=pc and briefly tell me the 3-4 most "
                 "important points in my language (temperatures, disks, memory, anything to watch out for). Also give "
                 "the path of the report file.")


def build(store: Store, days: int = 7, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    since = now - days * 86400
    prev = since - days * 86400
    out: dict = {"days": days, "since": since, "until": now, "rows": [], "procs": [], "disks": [], "alerts": []}
    for key, label, unit in (("pc.cpu.temp", _("Processor temperature"), "°C"), ("pc.gpu.temp", _("Graphics card temperature"), "°C"),
                             ("pc.cpu.percent", _("Processor usage"), "%"), ("pc.gpu.util", _("Graphics card usage"), "%"),
                             ("pc.mem.percent", _("Memory usage"), "%")):
        s = store.stats(key, since, now)
        if not s.count:
            continue
        p = store.stats(key, prev, since)
        delta = _(" (avg {delta} vs. the previous week)", delta=f"{s.avg - p.avg:+.1f}") if p.count else ""
        when = datetime.fromtimestamp(s.max_ts).strftime("%d.%m %H:%M") if s.max_ts else ""
        out["rows"].append((label, _("avg {avg}, highest {max} ({when})", avg=f"{s.avg:.0f}{unit}", max=f"{s.max:.0f}{unit}",
                                     when=when) + delta))
    for key in store.keys("pc.disk.*.percent", since=since):
        series = store.series(key, since, now)
        if series:
            name = key[len("pc.disk."):-len(".percent")]
            out["disks"].append((name, series[0][1], series[-1][1]))
    procs = []
    for key in store.keys("pc.proc.*.mem_mb", since=since):
        s = store.stats(key, since, now)
        if s.count >= 3:
            procs.append((key[len("pc.proc."):-len(".mem_mb")], s.avg, s.max, s.count))
    out["procs"] = sorted(procs, key=lambda p: p[1] * p[3], reverse=True)[:6]
    alerts = Counter(e.body or e.title for e in store.events(since=since, limit=1000) if e.kind == "alert")
    out["alerts"] = alerts.most_common(8)
    flights = alice_home() / "flights"
    out["flights"] = len([p for p in flights.glob("*.json") if p.stat().st_mtime >= since]) if flights.is_dir() else 0
    return out


def text(r: dict) -> str:
    lines = [_("Last {n} days:", n=r["days"])]
    lines += [f"- {k}: {v}" for k, v in r["rows"]]
    for name, a, b in r["disks"]:
        lines.append(_("- Disk {name}: {pct}% full ({delta} points)", name=name, pct=f"{b:.0f}", delta=f"{b - a:+.1f}"))
    if r["procs"]:
        lines.append(_("- Using the most memory: ") + ", ".join(_("{name} (avg {gb} GB)", name=n, gb=f"{a / 1024:.1f}")
                                                                 for n, a, _max, _count in r["procs"][:4]))
    if r["alerts"]:
        lines.append(_("- Alerts: ") + "; ".join(f"{t} ×{n}" for t, n in r["alerts"][:5]))
    else:
        lines.append(_("- No alerts"))
    if r["flights"]:
        lines.append(_("- Flights: {n}", n=r["flights"]))
    if len(lines) == 2 and not r["rows"]:
        lines.append(_("- Not enough data (is the core running?)"))
    return "\n".join(lines)


def save_html(r: dict, out: Path | None = None) -> Path:
    import html as h

    out = out or alice_home() / "reports"
    out.mkdir(parents=True, exist_ok=True)
    page = out / f"hafta-{datetime.fromtimestamp(r['until']):%Y-%m-%d}.html"
    rows = "".join(f"<tr><th>{h.escape(k)}</th><td>{h.escape(v)}</td></tr>" for k, v in r["rows"])
    disks = "".join(f"<tr><th>{h.escape(n)}</th><td><div class=bar><i style='width:{b:.0f}%'></i></div>"
                    f"%{b:.0f} ({b - a:+.1f})</td></tr>" for n, a, b in r["disks"])
    procs = "".join(f"<tr><th>{h.escape(n)}</th><td>"
                    + h.escape(_("avg {avg} GB, at most {max} GB", avg=f"{a / 1024:.1f}", max=f"{m / 1024:.1f}"))
                    + "</td></tr>" for n, a, m, _count in r["procs"])
    alerts = "".join(f"<li>{h.escape(t)} <span class=m>×{n}</span></li>" for t, n in r["alerts"]) or f"<li>{h.escape(_('No alerts'))}</li>"
    span = f"{datetime.fromtimestamp(r['since']):%d.%m} – {datetime.fromtimestamp(r['until']):%d.%m.%Y}"
    page.write_text(f"""<!doctype html><html lang="{language()}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{h.escape(_('Weekly report'))}</title><style>
:root{{--bg:#faf9f7;--fg:#222;--mut:#777;--line:#e4e1dc;--a:#c6613f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1b1a;--fg:#eee;--mut:#999;--line:#333;--a:#d97757}}}}
body{{background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif;margin:24px 16px;max-width:760px}}
th{{text-align:left;color:var(--mut);font-weight:400;padding:4px 16px 4px 0;vertical-align:top}} h2{{font-size:17px;margin-top:28px}}
.bar{{display:inline-block;width:160px;height:8px;background:var(--line);border-radius:4px;margin-right:8px}}
.bar i{{display:block;height:100%;background:var(--a);border-radius:4px}} .m{{color:var(--mut)}}
</style></head><body><h1>{h.escape(_('Weekly computer report'))}</h1><p class=m>{span}</p>
<h2>{h.escape(_('Overview'))}</h2><table>{rows}</table><h2>{h.escape(_('Disks'))}</h2><table>{disks}</table>
<h2>{h.escape(_('Using the most memory'))}</h2><table>{procs}</table><h2>{h.escape(_('Alerts'))}</h2><ul>{alerts}</ul></body></html>""",
                    encoding="utf-8")
    return page


def install(at: str = "20:00", weekday: int = 0):
    from alice.core.schedule import ScheduledTask, TaskStore

    h, m = (int(x) for x in at.split(":"))
    task = ScheduledTask(cron=f"{m} {h} * * {weekday}", prompt=REPORT_PROMPT, description=_("weekly computer report"),
                         catch_up=True, created=time.time(), id="weekly-report")
    return TaskStore().add(task)
