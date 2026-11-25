"""Listening test for voice selection.

Each candidate voice reads the same Turkish sentences with different emotions; the results are listened to
side by side on an HTML page. Local engines (XTTS, Chatterbox) clone from a reference recording, so their
candidacy is also an answer to "how well does it imitate the cloud voice?": the emotion-library idea rests on
this. Existing files are not regenerated (this preserves the quota).
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Callable

from alice.i18n import _, spoken
from alice.config import alice_home
from alice.voice.tone import EMOTIONS, NAMES_TR

SAMPLES = {
    "calm": "Good morning. It's a school day, your first class is at nine. The overnight backup finished fine.",
    "happy": "Great! The build passed on the first try, all the tests are green.",
    "serious": "The backup stopped halfway. The target disk is full; you need to free some space first.",
    "urgent": "Bingo fuel. At the current burn you have twelve minutes; head home now.",
    "playful": "It's three in the morning and Unity is open again. I don't sleep, but you should.",
}


def audition_dir() -> Path:
    return alice_home() / "voice" / "audition"


def run_audition(engines: list, emotions: list[str] | None = None, out: Path | None = None,
                 emit: Callable[[str], None] = lambda s: None, sleep: Callable[[float], None] = time.sleep,
                 max_wait: float = 120) -> Path:
    """engines: engines with synth(text, emotion) and name. Returns the index.html path.
    If the per-minute quota is used up, it waits and continues; if the daily quota is used up, it skips the
    remaining samples for that provider."""
    from alice.voice.tts import QuotaError

    exhausted: set[str] = set()
    out = out or audition_dir()
    out.mkdir(parents=True, exist_ok=True)
    emotions = emotions or list(EMOTIONS)
    results: dict[str, dict[str, str]] = {}
    for eng in engines:
        row = results.setdefault(eng.name, {})
        for emo in emotions:
            fname = f"{eng.name.replace(':', '_')}_{emo}.wav"
            path = out / fname
            provider = eng.name.split(":")[0]
            if not path.exists():
                if provider in exhausted:
                    row[emo] = "!" + _("daily quota used up")
                    continue
                emit(f"{eng.name} · {_(NAMES_TR[emo])}")
                err = None
                for _try in range(4):
                    try:
                        eng.synth(spoken(SAMPLES[emo]), emo).save(path)
                        err = None
                        model = getattr(eng, "last_model", "")
                        if model:  # which model and instruction format produced it (shown on the page)
                            row[f"{emo}@"] = f"{model} · {getattr(eng, 'last_format', '')}"
                        break
                    except QuotaError as q:
                        err = str(q)
                        if q.daily or not q.retry_after or q.retry_after > max_wait:
                            if q.daily:
                                exhausted.add(provider)
                            break
                        emit(f"  {q}; bekleniyor")
                        sleep(q.retry_after + 1)
                    except Exception as e:
                        err = str(e).splitlines()[0][:160]
                        break
                if err is not None:
                    emit("  " + _("skipped: {error}", error=err))
                    row[emo] = f"!{err}"
                    if provider in exhausted:
                        emit("  " + _("skipping the remaining samples for {provider}; run the same command tomorrow to continue", provider=provider))
                    continue
            row[emo] = fname
    # Also add the leftovers from previous runs to the page.
    meta = out / "results.json"
    try:
        old = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    for name, row in old.items():
        merged = results.setdefault(name, {})
        for emo, f in row.items():
            if emo not in merged or merged[emo].startswith("!"):
                merged[emo] = f
    meta.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    page = out / "index.html"
    page.write_text(render(results, emotions), encoding="utf-8")
    return page


def render(results: dict[str, dict[str, str]], emotions: list[str]) -> str:
    head = "".join(f"<th>{_(NAMES_TR[e])}<div class=s>{html.escape(spoken(SAMPLES[e]))}</div></th>" for e in emotions)
    rows = []
    for name, row in results.items():
        cells = []
        for e in emotions:
            f = row.get(e, "")
            if f and not f.startswith("!"):
                info = row.get(f"{e}@", "")
                cells.append(f'<td><audio controls preload="none" src="{html.escape(f)}"></audio>'
                             f'{f"<code>{html.escape(info)}</code>" if info else ""}</td>')
            else:
                cells.append(f'<td class=err>{html.escape(f[1:80]) if f else "-"}</td>')
        rows.append(f"<tr><td><b>{html.escape(name)}</b><code>alice voice use {html.escape(name)}</code></td>"
                    f"{''.join(cells)}</tr>")
    return f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ALICE ses seçimi</title>
<style>
:root{{--bg:#faf9f7;--fg:#222;--mut:#777;--line:#e4e1dc}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1b1a;--fg:#eee;--mut:#999;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif;margin:24px 16px}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid var(--line);padding:8px;vertical-align:top;text-align:left}}
th .s{{font-weight:400;color:var(--mut);font-size:12px;margin-top:4px}} code{{display:block;color:var(--mut);font-size:12px;margin-top:4px}}
audio{{width:190px}} .err{{color:#c55;font-size:12px}} .wrap{{overflow-x:auto}}
</style></head><body>
<h1>ALICE için ses seç</h1>
<p>Her satır bir aday. Aynı cümleleri farklı tonlarda dinle; beğendiğinin altındaki komutu çalıştır.
Yerel motorlar (xtts, chatterbox) bulut sesinden klonlar: ne kadar benzediklerine de kulak ver.</p>
<div class=wrap><table><tr><th>aday</th>{head}</tr>{''.join(rows)}</table></div>
</body></html>"""
