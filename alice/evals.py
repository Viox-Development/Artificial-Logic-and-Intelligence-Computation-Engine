"""`alice eval`: gerçek konuşmalardan test seti. ALICE'in saçmaladığı cümleleri sakla, her değişiklikten sonra tekrar
oynat, düzeldi mi bozuldu mu gör.

~/.alice/evals.toml:

  [[case]]
  say = "F-16 ile F-15 arasındaki fark ne"
  expect = "motor"                 # cevapta geçmeli (regex, büyük/küçük harf duyarsız)
  forbid = "bilmiyorum|emin değilim"
  expect_tools = ["web_search"]    # bunlardan en az biri çağrılmalı
  forbid_tools = ["run_command"]
  role = "brain"                   # brain (varsayılan) | voice | fast ya da "sağlayıcı:model"

Vakalar "ask" modunda çalışır ve her onay sorusu otomatik reddedilir: ALICE okur ve araştırır (web, dosya okuma),
ama değişiklik yapan, ekrana dokunan ya da mesaj gönderen araçlar ÇALIŞMAZ; yine de hangi aracı seçtiği kaydedilir
(doğru aracı mı seçti, görülür). Test konuşmaları hafızaya yazılmaz. Sonuçlar
~/.alice/eval-results.json'a yazılır; bir sonraki çalıştırma öncekiyle karşılaştırır.

  alice eval                       hepsini çalıştır
  alice eval --only f16            adında/cümlesinde "f16" geçenler
  alice eval --add "cümle"         yeni vaka (beklentileri dosyada doldur)
  alice eval --from-log 20         voice.log'daki son 20 cümleyi vaka taslağı olarak ekle
  alice eval --feedback            beğenmediğin cevaplar (sesle "bu cevap kötüydü" ya da uygulamada 👎)

Beğenmediğin her cevap (feedback aracı ya da uygulamadaki 👎) ~/.alice/feedback.jsonl'a yazılır ve isteğin, kötü
cevap ve notunla birlikte buraya vaka olarak eklenir; expect/forbid'i doldurunca her çalıştırmada denenir.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alice.config import alice_home


@dataclass
class Case:
    say: str
    expect: str = ""
    forbid: str = ""
    expect_tools: list[str] = field(default_factory=list)
    forbid_tools: list[str] = field(default_factory=list)
    role: str = "brain"
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or self.say[:50]


@dataclass
class Outcome:
    case: Case
    passed: bool
    reply: str
    tools: list[str]
    reasons: list[str]
    seconds: float
    model: str = ""


def evals_path() -> Path:
    return alice_home() / "evals.toml"


def results_path() -> Path:
    return alice_home() / "eval-results.json"


def load_cases(path: Path | None = None) -> list[Case]:
    path = path or evals_path()
    if not path.is_file():
        return []
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        import tomli as tomllib
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    out = []
    for raw in data.get("case", []) or []:
        if not isinstance(raw, dict) or not str(raw.get("say", "")).strip():
            continue
        out.append(Case(str(raw["say"]).strip(), str(raw.get("expect", "")), str(raw.get("forbid", "")),
                        [str(t) for t in raw.get("expect_tools", []) or []],
                        [str(t) for t in raw.get("forbid_tools", []) or []], str(raw.get("role", "brain") or "brain"),
                        str(raw.get("name", ""))))
    return out


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON dizgesi geçerli bir TOML temel dizgesidir


def add_cases(sayings: list[str], note: str = "", path: Path | None = None) -> int:
    path = path or evals_path()
    have = {c.say for c in load_cases(path)}
    new = [s for s in dict.fromkeys(x.strip() for x in sayings) if s and s not in have]
    if not new:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks = []
    for s in new:
        chunks.append(f"\n[[case]]\nsay = {_toml_str(s)}\n"
                      + (f"# {note}\n" if note else "")
                      + 'expect = ""          # cevapta geçmesi gereken (regex)\n'
                        'forbid = ""\n'
                        "expect_tools = []\n"
                        "forbid_tools = []\n")
    with path.open("a", encoding="utf-8") as f:
        if not path.stat().st_size:
            f.write("# ALICE test seti (alice eval). Ayrıntı: alice/evals.py\n")
        f.write("".join(chunks))
    return len(new)


def feedback_path() -> Path:
    return alice_home() / "feedback.jsonl"


def record_feedback(say: str, reply: str, note: str = "", source: str = "", path: Path | None = None) -> bool:
    """Beğenilmeyen cevabı kaydet: feedback.jsonl'a satır, evals.toml'a vaka (aynı istek zaten varsa eklenmez).
    Döner: yeni vaka eklendi mi."""
    say, reply, note = (say or "").strip(), (reply or "").strip(), (note or "").strip()
    try:
        with feedback_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "say": say, "reply": reply, "note": note, "source": source},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass
    if not say:
        return False
    one_line = lambda s: " ".join(s.split())[:300]  # noqa: E731
    comment = f"beğenilmedi ({source or '?'}): {one_line(reply)}" + (f" | not: {one_line(note)}" if note else "")
    return add_cases([say], note=comment, path=path) > 0


def recent_feedback(n: int = 20) -> list[dict[str, Any]]:
    try:
        lines = feedback_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


_LOG_RX = [re.compile(r"live \[[^\]]*\] duyulan: (.*?) \| cevap:"), re.compile(r"duyulan \[[^\]]*\](?: \(doğrudan\))?: (.*)$")]


def sayings_from_log(n: int = 20) -> list[str]:
    path = alice_home() / "voice.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for line in reversed(lines):
        if "gürültü sayıldı" in line:
            continue
        for rx in _LOG_RX:
            m = rx.search(line)
            if m and len(m.group(1).split()) >= 2:
                out.append(m.group(1).strip())
                break
        if len(out) >= n:
            break
    return list(reversed(out))


def check(case: Case, reply: str, tools: list[str]) -> list[str]:
    reasons = []
    if case.expect and not re.search(case.expect, reply, re.I):
        reasons.append(f"cevapta yok: {case.expect}")
    if case.forbid and re.search(case.forbid, reply, re.I):
        reasons.append(f"cevapta olmamalı: {case.forbid}")
    if case.expect_tools and not any(t in tools for t in case.expect_tools):
        reasons.append(f"beklenen araç çağrılmadı: {'/'.join(case.expect_tools)}")
    bad = [t for t in case.forbid_tools if t in tools]
    if bad:
        reasons.append(f"yasak araç çağrıldı: {', '.join(bad)}")
    if not reply.strip():
        reasons.append("boş cevap")
    return reasons


class _RecUI:
    def __init__(self):
        self.tools: list[str] = []
        self.models: list[str] = []

    def __getattr__(self, name: str):
        return lambda *a, **k: None

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self.tools.append(name)

    def stream_end(self, text: str, source: str) -> None:
        if source:
            self.models.append(source)

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        self.tools.append(name)  # seçtiği araç sayılır ama çalışmaz
        return "n"


def run_case(case: Case, make_agent) -> Outcome:
    """make_agent() -> konuşması sıfırlanmış bir Agent ("ask" modunda, hafızasız)."""
    agent = make_agent()
    ui = _RecUI()
    agent.ui = ui
    if case.role:
        agent.role = case.role
    t0 = time.monotonic()
    try:
        reply = agent.run_turn(case.say, max_steps=12) or ""
    except Exception as e:  # noqa: BLE001 - bir vaka çökse de set sürsün
        reply = f"HATA {type(e).__name__}: {e}"
    reasons = check(case, reply, ui.tools)
    return Outcome(case, not reasons, reply, ui.tools, reasons, time.monotonic() - t0, ui.models[-1] if ui.models else "")


def save_results(outcomes: list[Outcome]) -> dict[str, Any]:
    """Sonuçları yaz; öncekiyle karşılaştırma döndür: {'before': n, 'fixed': [...], 'broken': [...]}."""
    path = results_path()
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    prev_cases = {c["say"]: c["passed"] for c in (prev.get("cases") or [])}
    now = {"ts": time.time(), "passed": sum(o.passed for o in outcomes), "total": len(outcomes),
           "cases": [{"say": o.case.say, "passed": o.passed, "reasons": o.reasons, "tools": o.tools, "model": o.model,
                      "reply": o.reply[:600], "seconds": round(o.seconds, 1)} for o in outcomes]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(now, ensure_ascii=False, indent=1), encoding="utf-8")
    fixed = [o.case.label for o in outcomes if o.passed and prev_cases.get(o.case.say) is False]
    broken = [o.case.label for o in outcomes if not o.passed and prev_cases.get(o.case.say) is True]
    return {"before": prev.get("passed"), "before_total": prev.get("total"), "fixed": fixed, "broken": broken}


def eval_cmd(argv: list[str]) -> int:
    import argparse

    from rich.console import Console
    from rich.markup import escape

    console = Console()
    ap = argparse.ArgumentParser(prog="alice eval", description="Gerçek konuşmalardan test seti (bkz. alice/evals.py)")
    ap.add_argument("--only", help="adında ya da cümlesinde bu geçen vakalar")
    ap.add_argument("--add", action="append", help="yeni vaka cümlesi (tekrarlanabilir)")
    ap.add_argument("--from-log", type=int, metavar="N", help="voice.log'daki son N cümleyi vaka taslağı yap")
    ap.add_argument("--feedback", action="store_true", help="beğenmediğin son cevapları göster")
    a = ap.parse_args(argv)
    if a.feedback:
        rows = recent_feedback(30)
        if not rows:
            console.print("geri bildirim yok. Sesle 'bu cevap kötüydü' de ya da uygulamada cevabın altındaki 👎.")
            return 0
        for r in rows:
            when = time.strftime("%d.%m %H:%M", time.localtime(r.get("ts", 0)))
            console.print(f"[dim]{when} {escape(r.get('source', ''))}[/]  [bold]{escape(r.get('say', '') or '?')}[/]")
            console.print(f"   cevap: {escape(r.get('reply', '')[:200])}")
            if r.get("note"):
                console.print(f"   not: {escape(r['note'])}")
        console.print(f"\nvakalar: {evals_path()} (expect/forbid'i doldur, sonra: alice eval)")
        return 0
    if a.add or a.from_log:
        sayings = list(a.add or []) + (sayings_from_log(a.from_log) if a.from_log else [])
        n = add_cases(sayings, "voice.log'dan" if a.from_log else "")
        console.print(f"{n} vaka eklendi: {evals_path()} (beklentileri doldur, sonra: alice eval)")
        return 0
    cases = load_cases()
    if a.only:
        q = a.only.lower()
        cases = [c for c in cases if q in c.say.lower() or q in c.name.lower()]
    if not cases:
        console.print(f"vaka yok. Eklemek için: alice eval --add \"cümle\" ya da alice eval --from-log 20 "
                      f"({evals_path()})")
        return 1
    from pathlib import Path as _P

    from alice.cli import build
    from alice.config import load_config

    from alice.tools import cleanup

    cfg = load_config()
    agent, _, ctx = build(cfg, _P.home(), "ask")
    agent.memory = None  # test konuşmaları hafızaya kaydedilmesin (profil sistem isteminde kalır)
    base_role = agent.role

    def make_agent():
        agent.messages[:] = agent.messages[:1]
        agent.role = base_role
        return agent

    outcomes = []
    try:
        for i, c in enumerate(cases, 1):
            o = run_case(c, make_agent)
            outcomes.append(o)
            mark = "[green]geçti[/]" if o.passed else "[red]kaldı[/]"
            console.print(f"{i:>2}. {mark} {escape(c.label)} [dim]({o.seconds:.1f} sn, {escape(o.model)}, "
                          f"araçlar: {escape(', '.join(o.tools) or '-')})[/]")
            for r in o.reasons:
                console.print(f"     [yellow]{escape(r)}[/]")
            if not o.passed:
                console.print(f"     [dim]cevap: {escape(o.reply[:200])}[/]")
    finally:
        cleanup(ctx)
    cmp = save_results(outcomes)
    passed = sum(o.passed for o in outcomes)
    line = f"\n{passed}/{len(outcomes)} geçti"
    if cmp["before"] is not None:
        line += f" (önceki çalıştırma: {cmp['before']}/{cmp['before_total']})"
    console.print(line)
    if cmp["fixed"]:
        console.print("[green]düzelenler:[/] " + escape(", ".join(cmp["fixed"])))
    if cmp["broken"]:
        console.print("[red]bozulanlar:[/] " + escape(", ".join(cmp["broken"])))
    return 0 if passed == len(outcomes) else 1
