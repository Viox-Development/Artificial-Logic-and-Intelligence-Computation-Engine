"""Çok adımlı görev yürütme: tek bir istekten plan çıkar, adımları sırayla yap, her adımı doğrula.

  1. plan     -> beyin modeli isteği 2-8 adıma böler (JSON). Her adımın hedefi, nasıl doğrulanacağı ve geri
                 alınamaz olup olmadığı (mesaj göndermek, silmek, ödemek...) yazılır. İstek belirsizse tek soru döner.
  2. yürüt    -> her adım için temiz bağlamlı bir alt ajan sadece o adımı yapar; önceki adımların sonuçlarını görür.
                 Riskli adımdan önce kullanıcıdan onay alınır (sesli konuşmada sesle).
  3. doğrula  -> adımın sonucu ayrı, sadece okuyabilen bir alt ajanla kontrol edilir (ekran/sayfa/dosya). Olmadıysa
                 adım bir kez farklı yoldan tekrar denenir; yine olmazsa görev durur ve ne olduğu raporlanır.

Sesli konuşmada (Live) görev arka planda sürer; ilerleme ve onay soruları konuşmaya yazılır, kullanıcının cevabı
task_control aracıyla gelir. Araçlar, tarayıcı gibi iş parçacığına bağlı nesneler yüzünden Live'ın araç iş
parçacığında çalıştırılır (ctx.state["task_hooks"]["run_tool"]).
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from alice.tools.base import DANGER, Tool, ToolContext, ToolResult

PLANNER = """Sen bir görev planlayıcısısın. Kullanıcının isteğini, bilgisayarda sırayla yapılacak 2-{max_steps} somut
adıma böl. Her adım tek başına yapılabilir ve sonucu gözle kontrol edilebilir olsun. Kullanıcının bilgisayarı Windows;
tarayıcısı açık ve bağlanılabilir. Kullanılabilecek araçlar: {tools}.

Sadece JSON döndür, başka hiçbir şey yazma:
{{"steps": [{{"goal": "<adım, Türkçe, net ve eksiksiz: hangi uygulama/site, ne yazılacak>",
             "check": "<adım bitince ekranda/sayfada/dosyada görülmesi gereken şey; doğrulanması gereksizse boş>",
             "risky": <true|false>, "risk": "<risky ise neden: kime ne gidecek, ne silinecek>"}}],
 "question": "<istek belirsizse kullanıcıya sorulacak TEK kısa soru, değilse boş>"}}

Kurallar:
- risky=true: mesaj/e-posta/yorum göndermek ya da paylaşmak, dosya/kayıt silmek ya da üzerine yazmak, satın almak,
  ödemek, bir şey kurmak/kaldırmak, sistem ayarı değiştirmek, kaydedilmemiş işi kapatmak gibi geri alınamayan her şey.
  Sadece açmak, aramak, okumak, bakmak, kopyalamak risky değildir.
- Kullanıcının söylemediği işi plana ekleme. İstek tek adımlık ise tek adım yaz.
- Kimin/neyin kastedildiği belli değilse ve yanlış tahmin geri alınamaz bir şeye yol açacaksa question yaz.
- Şifre girme, ödeme bilgisi doldurma adımı yazma; gerekiyorsa o adımda kullanıcıya bırak."""

STEP = """[Çok adımlı görev] Ana hedef: {goal}
Plan:
{plan}
Önceki adımların sonuçları:
{done}
{retry}Şimdi SADECE {n}. adımı yap: {step}
Araçları kullanarak gerçekten yap; sonraki adımlara geçme, kullanıcıya soru sorma.
Bitince son satıra tek satır yaz: "SONUÇ: TAMAM - <ne oldu, sonraki adımlar için lazım olan bilgi (ad, yol, link)>"
ya da yapamadıysan "SONUÇ: OLMADI - <neden>"."""

VERIFY = """[Doğrulama] Bir bilgisayar adımının gerçekten yapılıp yapılmadığını kontrol et. HİÇBİR ŞEYİ DEĞİŞTİRME:
sadece bakan araçları kullan (browser snapshot/text/tabs, ekran görüntüsü, ui_elements, windows list, read_file,
list_dir...). Yürütücünün raporuna körü körüne güvenme; mümkünse kendin bak.
Adım: {step}
Beklenen: {check}
Yürütücünün raporu: {report}
Son satıra tek satır yaz: "DOĞRULAMA: EVET - <ne gördün>" ya da "DOĞRULAMA: HAYIR - <ne eksik>"."""

# Planlayıcı riskli adımı işaretlemeyi unutsa bile bu kelimeler geçen adım için onay sorulur.
RISKY_RX = re.compile(
    r"\bgönder(\b|in\b|ip\b|eyim)|\byolla(\b|yıp)|\bpaylaş(\b|ıp)|\byayınla(\b|yıp)|\bsil(\b|in\b|ip\b)|"
    r"\bkaldır(\b|ın\b|ıp\b)|üzerine yaz|satın al|sipariş ver|\böde(\b|me yap|yip)|\bkur(\b|ulum)|"
    r"\bkaldır|biçimlendir|\bformatla|kaydetmeden kapat|"
    r"\b(send|delete|remove|uninstall|install|purchase|buy|pay|publish)\b", re.I)

# Görevin içindeki alt ajanların göremeyeceği araçlar: iç içe görev ve görev kontrolü anlamsız.
HIDDEN = {"run_plan", "task_control", "task"}


@dataclass
class Step:
    goal: str
    check: str = ""
    risky: bool = False
    risk: str = ""
    status: str = "pending"  # pending | running | done | failed | skipped
    result: str = ""
    tries: int = 0


@dataclass
class Plan:
    goal: str
    steps: list[Step] = field(default_factory=list)
    question: str = ""


def _first_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?", "", text or "")
    m = re.search(r"\{.*\}", cleaned, re.S)
    if not m:
        raise ValueError(f"planda JSON yok: {(text or '')[:160]}")
    val = json.loads(m.group(0))
    if not isinstance(val, dict):
        raise ValueError("plan JSON obje değil")
    return val


def parse_plan(goal: str, text: str, max_steps: int = 8) -> Plan:
    data = _first_json(text)
    steps = []
    for s in data.get("steps") or []:
        if isinstance(s, str):
            s = {"goal": s}
        g = str(s.get("goal") or "").strip()
        if not g:
            continue
        risky = bool(s.get("risky")) or bool(RISKY_RX.search(g))
        steps.append(Step(goal=g, check=str(s.get("check") or "").strip(), risky=risky,
                          risk=str(s.get("risk") or "").strip()))
    question = str(data.get("question") or "").strip()
    if not steps and not question:
        raise ValueError("planda adım yok")
    return Plan(goal=goal, steps=steps[:max_steps], question=question)


def outcome(text: str, key: str, yes: str) -> tuple[bool | None, str]:
    """Ajan cevabının son "<key>: <yes|...> - açıklama" satırını oku. Satır yoksa (None, metin)."""
    for line in reversed((text or "").strip().splitlines()):
        m = re.match(rf"\s*\**{key}\**\s*:\s*\**\s*(\w+)\s*\**\s*[-–—:]?\s*(.*)", line, re.I)
        if m:
            return m.group(1).upper().replace("İ", "I") == yes, m.group(2).strip() or line.strip()
    return None, (text or "").strip()[-400:]


class _TaskUI:
    """Alt ajanların arayüzü: araç olaylarını görev günlüğüne yazar, izin sorularını görevin onay yoluna yollar."""

    def __init__(self, runner: "TaskRunner"):
        self.runner = runner

    def __getattr__(self, name: str):
        return lambda *a, **k: None

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self.runner.log(f"  araç {name}({summary[:100]})")

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        if result.is_error:
            self.runner.log(f"  araç hatası {name}: {result.text[:160]}")

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        if self.runner.step_approved and risk != DANGER:
            return "y"
        return "y" if self.runner.confirm(f"{name} çalıştırılsın mı: {summary[:160]}") else "n"


class TaskRunner:
    def __init__(self, ctx: ToolContext, goal: str, max_steps: int = 8, step_tool_calls: int = 14,
                 answer_timeout: float = 120.0, log: Callable[[str], None] | None = None):
        self.ctx = ctx
        self.goal = goal
        self.max_steps = max_steps
        self.step_tool_calls = step_tool_calls
        self.answer_timeout = answer_timeout
        self.plan: Plan | None = None
        self.stopped = threading.Event()
        self.step_approved = False
        self.waiting: str = ""  # kullanıcıdan beklenen onayın sorusu
        self._answer: bool | None = None
        self._answered = threading.Event()
        self._child: Any = None
        self._log = log or (lambda s: None)
        self.lines: list[str] = []

    # ------------------------------------------------------------- kancalar
    @property
    def hooks(self) -> dict[str, Any]:
        return self.ctx.state.get("task_hooks") or {}

    def log(self, msg: str) -> None:
        self.lines.append(msg)
        self._log(msg)
        try:
            self.ctx.emit(msg.strip())
        except Exception:  # noqa: BLE001
            pass

    def say(self, text: str) -> None:
        """Kullanıcıya duyur: sesli konuşmada Live'a yazılır (Alice söyler), yoksa arayüze bilgi olarak gider."""
        self.log(f"duyuru: {text}")
        fn = self.hooks.get("say")
        if fn is not None:
            try:
                fn(text)
                return
            except Exception:  # noqa: BLE001 - konuşma kapanmış
                pass
        agent = self.ctx.state.get("agent")
        info = getattr(getattr(agent, "ui", None), "info", None)
        if info is not None:
            info(text)

    def confirm(self, question: str) -> bool:
        """Riskli bir adımdan önce kullanıcıya sor. Sesli konuşmada cevap task_control ile gelir; gelmezse hayır."""
        if self.stopped.is_set():
            return False
        self.log(f"onay soruluyor: {question}")
        say = self.hooks.get("say")
        if say is None:
            agent = self.ctx.state.get("agent")
            ask = getattr(getattr(agent, "ui", None), "ask_permission", None)
            ok = ask is not None and ask("run_plan", question, DANGER) in ("y", "a", "p")
            self.log("onay: " + ("evet" if ok else "hayır"))
            return ok
        self._answer, self.waiting = None, question
        self._answered.clear()
        try:
            say(f"[görev onay bekliyor] Kullanıcıya kısaca sor: {question} Evet derse task_control action=approve, "
                "hayır derse action=reject çağır. Kendi kendine onaylama.")
        except Exception:  # noqa: BLE001
            self.waiting = ""
            return False
        deadline = time.monotonic() + self.answer_timeout
        while not self._answered.wait(0.2):
            if self.stopped.is_set() or time.monotonic() > deadline:
                break
        self.waiting = ""
        ok = bool(self._answer)
        self.log("onay: " + ("evet" if ok else "hayır" if self._answered.is_set() else "cevap gelmedi"))
        return ok

    def answer(self, approve: bool) -> bool:
        if not self.waiting:
            return False
        self._answer = approve
        self._answered.set()
        return True

    def stop(self) -> None:
        self.stopped.set()
        self._answered.set()
        if self._child is not None:
            try:
                self._child.cancel.set()
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- alt ajan
    def _agent(self, verify: bool = False):
        from alice.agent import Agent
        from alice.permissions import Permissions

        parent = self.ctx.state.get("agent")
        if parent is None or self.ctx.router is None:
            raise RuntimeError("ajan yok")
        tools = [t for n, t in parent.tools.items() if n not in HIDDEN]
        perms = Permissions("plan") if verify else parent.permissions
        child = Agent(parent.config, parent.router, tools, self.ctx, perms, _TaskUI(self), parent.decider,
                      system_prompt=parent.messages[0]["content"], is_child=True)
        child.stream = False
        child.role = "brain"
        run_tool = self.hooks.get("run_tool")
        if run_tool is not None:  # tarayıcı/UIA nesneleri Live'ın araç iş parçacığına bağlı: araçlar orada çalışsın
            def on_tool_thread(tool: Tool, args: dict[str, Any], _child=child) -> ToolResult:
                return run_tool(lambda: Agent._run_tool(_child, tool, args))

            child._run_tool = on_tool_thread
        self._child = child
        return child

    def _ask(self, prompt: str, verify: bool = False, steps: int | None = None) -> str:
        child = self._agent(verify)
        try:
            return child.run_turn(prompt, max_steps=steps or self.step_tool_calls) or ""
        finally:
            self._child = None

    # ----------------------------------------------------------------- plan
    def make_plan(self) -> Plan:
        parent = self.ctx.state.get("agent")
        names = sorted(n for n in getattr(parent, "tools", {}) if n not in HIDDEN and not n.startswith("mcp__"))
        prompt = PLANNER.format(max_steps=self.max_steps, tools=", ".join(names) or "(bilinmiyor)")
        last: Exception | None = None
        for _ in range(2):
            res = self.ctx.router.chat("brain", [{"role": "system", "content": prompt},
                                                 {"role": "user", "content": self.goal}], max_tokens=1500)
            try:
                return parse_plan(self.goal, res.text, self.max_steps)
            except (ValueError, json.JSONDecodeError) as e:
                last = e
        raise RuntimeError(f"plan çıkarılamadı: {last}")

    def _plan_text(self, current: int) -> str:
        assert self.plan is not None
        mark = {"done": "[x]", "failed": "[!]", "skipped": "[-]", "running": "[~]"}
        return "\n".join(f"{mark.get(s.status, '[ ]')} {i}. {s.goal}" + ("  <- ŞİMDİKİ" if i == current else "")
                         for i, s in enumerate(self.plan.steps, 1))

    # ------------------------------------------------------------- yürütme
    def _do_step(self, n: int, step: Step, retry: str = "") -> tuple[bool, str]:
        assert self.plan is not None
        done = "\n".join(f"{i}. {s.result}" for i, s in enumerate(self.plan.steps, 1)
                         if s.status == "done" and s.result) or "(yok)"
        report = self._ask(STEP.format(goal=self.goal, plan=self._plan_text(n), done=done, n=n, step=step.goal,
                                       retry=f"Önceki deneme olmadı: {retry}. Farklı bir yol dene.\n" if retry else ""))
        ok, why = outcome(report, "SONUÇ", "TAMAM")
        if ok is False or self.stopped.is_set():
            return False, why
        if not step.check:
            return True, why
        verdict = self._ask(VERIFY.format(step=step.goal, check=step.check, report=why[:600]), verify=True, steps=6)
        vok, vwhy = outcome(verdict, "DOĞRULAMA", "EVET")
        if vok is False:
            return False, f"doğrulanamadı: {vwhy}"
        return True, why

    def run(self) -> str:
        self.log(f"görev: {self.goal}")
        try:
            self.plan = self.make_plan()
        except Exception as e:  # noqa: BLE001
            self.log(f"plan hatası: {e}")
            return f"error: {e}"
        plan = self.plan
        if plan.question:
            return (f"needs_info: {plan.question}\nKullanıcıya bu soruyu sor; cevabıyla run_plan'ı netleşmiş hedefle "
                    "yeniden çağır.")
        self.log("plan:\n" + self._plan_text(0))
        if len(plan.steps) > 1:
            self.say(f"[görev planı, kullanıcıya tek cümleyle söyle; liste okuma] {len(plan.steps)} adım: "
                     + "; ".join(s.goal for s in plan.steps))
        for n, step in enumerate(plan.steps, 1):
            if self.stopped.is_set():
                break
            self.step_approved = False
            if step.risky:
                why = f" ({step.risk})" if step.risk else ""
                if not self.confirm(f"{n}. adım geri alınamaz: {step.goal}{why}. Yapayım mı?"):
                    step.status, step.result = "skipped", "kullanıcı onaylamadı"
                    self.log(f"{n}. adım atlandı: onay yok")
                    break
                self.step_approved = True
            step.status = "running"
            self.log(f"{n}. adım: {step.goal}")
            reason = ""
            ok = False
            while step.tries < 2 and not self.stopped.is_set():
                step.tries += 1
                try:
                    ok, reason = self._do_step(n, step, retry=reason if step.tries > 1 else "")
                except Exception as e:  # noqa: BLE001 - model/araç çöktü: adımı tekrar dene
                    ok, reason = False, f"{type(e).__name__}: {e}"
                if ok:
                    break
                self.log(f"{n}. adım olmadı ({step.tries}. deneme): {reason[:200]}")
                if step.risky:  # geri alınamaz adımı kendiliğinden tekrarlama: iki kez göndermesin
                    break
            step.result = reason[:500]
            step.status = "done" if ok else "failed"
            self.log(f"{n}. adım {'tamam' if ok else 'olmadı'}: {step.result[:200]}")
            if not ok:
                break
        self.step_approved = False
        return self.report()

    def report(self) -> str:
        assert self.plan is not None
        steps = self.plan.steps
        ok = sum(s.status == "done" for s in steps)
        if self.stopped.is_set():
            status = "stopped"
        elif ok == len(steps):
            status = "done"
        else:
            status = "fail"
        lines = [f"{status}: {ok}/{len(steps)} adım tamam"]
        for i, s in enumerate(steps, 1):
            mark = {"done": "✓", "failed": "✗", "skipped": "-", "running": "~"}.get(s.status, " ")
            lines.append(f"{mark} {i}. {s.goal}" + (f" -> {s.result}" if s.result else ""))
        if status != "done":
            lines.append("Kullanıcıya hangi adımda neden durduğunu söyle; yapılmamış adımları yapıldı deme.")
        return "\n".join(lines)

    def status_text(self) -> str:
        head = f"görev: {self.goal}"
        if self.waiting:
            head += f"\nonay bekliyor: {self.waiting}"
        return head + "\n" + (self._plan_text(0) if self.plan is not None else "planlanıyor")


def task_log(text: str) -> None:
    from alice.config import alice_home

    try:
        with open(alice_home() / "tasks.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass
