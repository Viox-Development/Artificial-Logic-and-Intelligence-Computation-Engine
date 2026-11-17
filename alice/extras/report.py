"""Weekly computer report: temperatures, memory, disks, top resource users, alerts."""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from alice.config import alice_home
from alice.core.store import Store

REPORT_PROMPT = ("Haftalık bilgisayar raporumu hazırla: report aracını kind=pc ile çağır ve en önemli 3-4 noktayı kısaca anlat "
                 "(sıcaklık, disk, bellek, dikkat edilmesi gereken bir şey varsa). Rapor dosyasının yolunu da söyle.")


def build(store: Store, days: int = 7, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    since = now - days * 86400
    prev = since - days * 86400
    out: dict = {"days": days, "since": since, "until": now, "rows": [], "procs": [], "disks": [], "alerts": []}
    for key, label, unit in (("pc.cpu.temp", "İşlemci sıcaklığı", "°C"), ("pc.gpu.temp", "Ekran kartı sıcaklığı", "°C"),
                             ("pc.cpu.percent", "İşlemci kullanımı", "%"), ("pc.gpu.util", "Ekran kartı kullanımı", "%"),
                             ("pc.mem.percent", "Bellek kullanımı", "%")):
        s = store.stats(key, since, now)
        if not s.count:
            continue
        p = store.stats(key, prev, since)
        delta = f" (önceki haftaya göre ort {s.avg - p.avg:+.1f})" if p.count else ""
        when = datetime.fromtimestamp(s.max_ts).strftime("%d.%m %H:%M") if s.max_ts else ""
        out["rows"].append((label, f"ort {s.avg:.0f}{unit}, en yüksek {s.max:.0f}{unit} ({when}){delta}"))
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
    lines = [f"Son {r['days']} gün:"]
    lines += [f"- {k}: {v}" for k, v in r["rows"]]
    for name, a, b in r["disks"]:
        lines.append(f"- Disk {name}: %{b:.0f} dolu ({b - a:+.1f} puan)")
    if r["procs"]:
        lines.append("- En çok bellek kullananlar: " + ", ".join(f"{n} (ort {a / 1024:.1f} GB)" for n, a, _, _ in r["procs"][:4]))
    if r["alerts"]:
        lines.append("- Uyarılar: " + "; ".join(f"{t} ×{n}" for t, n in r["alerts"][:5]))
    else:
        lines.append("- Uyarı yok")
    if r["flights"]:
        lines.append(f"- Uçuş sayısı: {r['flights']}")
    if len(lines) == 2 and not r["rows"]:
        lines.append("- Yeterli veri yok (çekirdek çalışıyor mu?)")
    return "\n".join(lines)


def save_html(r: dict, out: Path | None = None) -> Path:
    import html as h

    out = out or alice_home() / "reports"
    out.mkdir(parents=True, exist_ok=True)
    page = out / f"hafta-{datetime.fromtimestamp(r['until']):%Y-%m-%d}.html"
    rows = "".join(f"<tr><th>{h.escape(k)}</th><td>{h.escape(v)}</td></tr>" for k, v in r["rows"])
    disks = "".join(f"<tr><th>{h.escape(n)}</th><td><div class=bar><i style='width:{b:.0f}%'></i></div>"
                    f"%{b:.0f} ({b - a:+.1f})</td></tr>" for n, a, b in r["disks"])
    procs = "".join(f"<tr><th>{h.escape(n)}</th><td>ort {a / 1024:.1f} GB, en fazla {m / 1024:.1f} GB</td></tr>"
                    for n, a, m, _ in r["procs"])
    alerts = "".join(f"<li>{h.escape(t)} <span class=m>×{n}</span></li>" for t, n in r["alerts"]) or "<li>Uyarı yok</li>"
    span = f"{datetime.fromtimestamp(r['since']):%d.%m} – {datetime.fromtimestamp(r['until']):%d.%m.%Y}"
    page.write_text(f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Haftalık rapor</title><style>
:root{{--bg:#faf9f7;--fg:#222;--mut:#777;--line:#e4e1dc;--a:#c6613f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1b1a;--fg:#eee;--mut:#999;--line:#333;--a:#d97757}}}}
body{{background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif;margin:24px 16px;max-width:760px}}
th{{text-align:left;color:var(--mut);font-weight:400;padding:4px 16px 4px 0;vertical-align:top}} h2{{font-size:17px;margin-top:28px}}
.bar{{display:inline-block;width:160px;height:8px;background:var(--line);border-radius:4px;margin-right:8px}}
.bar i{{display:block;height:100%;background:var(--a);border-radius:4px}} .m{{color:var(--mut)}}
</style></head><body><h1>Haftalık bilgisayar raporu</h1><p class=m>{span}</p>
<h2>Genel</h2><table>{rows}</table><h2>Diskler</h2><table>{disks}</table>
<h2>En çok bellek kullananlar</h2><table>{procs}</table><h2>Uyarılar</h2><ul>{alerts}</ul></body></html>""",
                    encoding="utf-8")
    return page


def install(at: str = "20:00", weekday: int = 0):
    from alice.core.schedule import ScheduledTask, TaskStore

    h, m = (int(x) for x in at.split(":"))
    task = ScheduledTask(cron=f"{m} {h} * * {weekday}", prompt=REPORT_PROMPT, description="haftalık bilgisayar raporu",
                         catch_up=True, created=time.time(), id="weekly-report")
    return TaskStore().add(task)
