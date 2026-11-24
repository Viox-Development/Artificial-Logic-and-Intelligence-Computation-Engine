"""Extra commands.

  alice notes [add "metin" (text) | search "kelime" (word) [süre (duration)] | undo]
  alice brief [--install 08:00] [--weekdays]     morning summary (show now, or install for every morning)
  alice report [--days 7] [--open] [--install 20:00]   weekly computer report (installed on Sunday evenings)
"""

from __future__ import annotations

import webbrowser

from rich.console import Console
from rich.markup import escape

from alice.i18n import _

console = Console(highlight=False)


def notes_cmd(argv: list[str]) -> int:
    from alice.core.store import parse_time
    from alice.extras import notes

    sub = argv[0] if argv else "list"
    if sub == "add" and len(argv) > 1:
        notes.add(" ".join(argv[1:]))
        console.print(_("[green]note saved[/]"))
        return 0
    if sub == "undo":
        n = notes.remove_last()
        console.print(_("deleted: {text}", text=escape(n.text)) if n else _("[dim]no notes[/]"))
        return 0
    if sub == "search" and len(argv) > 1:
        found = notes.search(argv[1], parse_time(argv[2]) if len(argv) > 2 else None)
    else:
        found = notes.all_notes()[-30:]
    for n in found:
        console.print(escape(n.line()))
    if not found:
        console.print(_("[dim]no notes[/]"))
    return 0


def brief_cmd(argv: list[str]) -> int:
    from alice.config import load_config
    from alice.core.schedule import TaskStore
    from alice.core.store import Store
    from alice.extras import brief

    if "--install" in argv:
        i = argv.index("--install")
        at = argv[i + 1] if len(argv) > i + 1 and ":" in argv[i + 1] else "08:00"
        t = brief.install(at, weekdays_only="--weekdays" in argv)
        console.print(_("[green]set up:[/] {summary}", summary=escape(t.summary())))
        return 0
    cfg = load_config()
    with Store() as st:
        console.print(escape(brief.render(brief.gather(cfg.data.get("brief", {}), st, TaskStore()))))
    return 0


def report_cmd(argv: list[str]) -> int:
    from alice.core.store import Store
    from alice.extras import report

    if "--install" in argv:
        i = argv.index("--install")
        at = argv[i + 1] if len(argv) > i + 1 and ":" in argv[i + 1] else "20:00"
        t = report.install(at)
        console.print(_("[green]set up:[/] {summary}", summary=escape(t.summary())))
        return 0
    days = 7
    if "--days" in argv:
        val = argv[argv.index("--days") + 1] if argv.index("--days") + 1 < len(argv) else ""
        if not val.isdigit() or int(val) < 1:
            console.print(_("--days must be a number of days, e.g. alice report --days 7 (given: {value})",
                            value=val or _("nothing")),
                          markup=False)
            return 2
        days = int(val)
    with Store() as st:
        r = report.build(st, days)
    console.print(escape(report.text(r)))
    page = report.save_html(r)
    console.print(f"[dim]{page}[/]")
    if "--open" in argv:
        webbrowser.open(page.as_uri())
    return 0
