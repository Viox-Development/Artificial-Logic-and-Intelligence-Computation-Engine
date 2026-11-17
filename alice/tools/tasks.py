"""Multi-step task tools: run_plan (plan, execute, verify) and task_control (approval, stop, status)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, READ, Tool, ToolContext, ToolError, ToolResult, obj


class RunPlan(Tool):
    name = "run_plan"
    description = (
        "Birkaç ayrı adımdan oluşan bir isteği planlayıp baştan sona yapar: 2-8 adıma böler, her adımı araçlarla "
        "yapar ve sonucunu kontrol eder; olmayan adımı bir kez başka yoldan dener. Geri alınamaz adımlardan "
        "(mesaj/e-posta göndermek, silmek, satın almak, kurmak) önce kullanıcıya onay sorar (cevap task_control "
        "ile verilir). Dakikalar sürebilir.\n"
        "- Kullan: 'Gmail'deki son faturayı indir, masaüstüne kaydet ve Ahmet'e WhatsApp'tan gönder'.\n"
        "- Kullanma: tek araçla bitecek işler (doğrudan o aracı çağır); tek bir uygulamada ekran işi "
        "(computer_task). Aynı anda tek görev çalışır."
    )
    parameters = obj({"goal": {"type": "string", "description": "Kullanıcının isteği eksiksiz, kişi, dosya ve "
                               "uygulama adlarıyla; konuşmayı görmeyen birinin anlayacağı gibi."}}, ["goal"])
    risk = COMPUTER  # each tool call inside it goes through its own permission check

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("goal", ""))[:150]

    def run(self, ctx: ToolContext, goal: str) -> ToolResult:
        from alice.multistep import TaskRunner, task_log

        if not (goal or "").strip():
            raise ToolError("goal boş")
        if ctx.state.get("active_task") is not None:
            return ToolResult("zaten bir görev sürüyor; bitmesini bekle ya da task_control action=stop ile durdur.",
                              is_error=True)
        a = ctx.config.agent
        hooks = ctx.state.get("task_hooks") or {}
        log = hooks.get("log") or (lambda s: None)

        def both(s: str) -> None:
            task_log(s)
            log(f"görev: {s}")

        runner = TaskRunner(ctx, goal.strip(), max_steps=int(a.get("plan_max_steps", 8)),
                            step_tool_calls=int(a.get("plan_step_tool_calls", 14)),
                            answer_timeout=float(a.get("plan_answer_timeout", 120)), log=both)
        ctx.state["active_task"] = runner
        try:
            text = runner.run()
        finally:
            ctx.state.pop("active_task", None)
        both(f"bitti: {text.splitlines()[0] if text else ''}")
        return ToolResult(text, is_error=text.startswith("error:"))


class TaskControl(Tool):
    name = "task_control"
    description = ("Süren bir run_plan görevini yönetir: kullanıcının onayını ya da reddini iletir, görevi durdurur ya da "
                   "durumunu döndürür.\n"
                   "- approve/reject sadece kullanıcı bu konuşmada açıkça 'evet'/'hayır' dediğinde. Onayı ASLA kendin verme ya "
                   "da tahmin etme.\n"
                   "- 'Dur', 'vazgeç' denince stop; 'ne durumda' sorusunda status.\n"
                   "- Süren bir run_plan görevi yoksa hata döner.")
    parameters = obj({"action": {"type": "string", "enum": ["approve", "reject", "stop", "status"],
                                 "description": "approve: görev onay sorunca kullanıcı 'evet' dedi | reject: 'hayır' "
                                 "dedi | stop: 'dur', 'vazgeç' | status: 'ne durumda'."}}, ["action"])
    risk = READ

    def run(self, ctx: ToolContext, action: str) -> ToolResult:
        runner = ctx.state.get("active_task")
        if runner is None:
            return ToolResult("şu an süren bir görev yok")
        if action == "status":
            return ToolResult(runner.status_text())
        if action == "stop":
            runner.stop()
            return ToolResult("görev durduruluyor; o anki adım biter bitmez duracak")
        if action in ("approve", "reject"):
            if not runner.answer(action == "approve"):
                return ToolResult("görev şu an onay beklemiyor", is_error=True)
            return ToolResult("onaylandı, devam ediyor" if action == "approve" else "reddedildi, o adım yapılmayacak")
        raise ToolError(f"bilinmeyen action: {action}")


AGENT_SPEC = obj({
    "name": {"type": "string", "description": "Kısa ad, ör. 'backend', 'tests', 'ui'."},
    "task": {"type": "string", "description": "Yapacağı iş, konuşmayı görmeyen biri anlayacak kadar ayrıntılı: hangi "
             "dosyalar, ne değişecek, nasıl doğrulayacak, ne raporlayacak."},
    "constraints": {"type": "string", "description": "Sınırlar: dokunmayacağı yerler, uyacağı kurallar."},
    "scope": {"type": "array", "items": {"type": "string"},
              "description": "Yazabileceği dosya/klasörler (çalışma klasörüne göre). Boş: her yer. Çakışmasın."},
    "model": {"type": "string", "description": "brain (güçlü), fast (hızlı/ucuz) ya da sağlayıcı:model."},
    "thinking": {"type": "string", "enum": ["off", "low", "medium", "high", "default"],
                 "description": "Düşünme seviyesi: zor tasarım/hata ayıklama high, rutin iş low/off."},
    "isolation": {"type": "string", "enum": ["shared", "worktree", "readonly"],
                  "description": "shared: aynı klasör | worktree: kendi git dalı (onayda birleştirilir) | readonly: "
                  "sadece inceleme/araştırma."},
    "after": {"type": "array", "items": {"type": "string"}, "description": "Bu ajanlar onaylanınca başlasın."},
    "max_steps": {"type": "integer", "description": "En çok araç adımı. Varsayılan 60."},
}, ["name", "task"])


class TeamTool(Tool):
    name = "team"
    description = (
        "Büyük bir işi otonom bir PROJE olarak başlatır: projenin kendi yöneticisi (sen, arka planda) işi paralel "
        "çalışan ajanlara böler, sorularını cevaplar, biten her işi inceler (onaylar ya da düzeltmeye gönderir), "
        "sonunda doğrulayıp özet yazar. Kullanıcı ilerlemeyi uygulamadaki Projeler sayfasında izler.\n"
        "- Kullan: birbirinden ayrılabilen parçaları olan büyük işler (büyük kod tabanında birden çok modül, "
        "frontend+backend+test, geniş inceleme/araştırma). Küçük işlerde kullanma: kendin yap ya da task kullan.\n"
        "- start: goal'a kullanıcının isteğini, bildiklerini ve bitti ölçütünü yaz. agents vermezsen yönetici kodu "
        "inceleyip ajanları kendisi kurar (çoğu zaman en iyisi). Sonra kullanıcıya projenin başladığını söyle; "
        "sonucu sohbette istiyorsa action=wait ile bekle.\n"
        "- Ajanlar kod işi araçlarını kullanır (dosya, kabuk, web, git); ekran ve tarayıcı araçları onlara kapalı."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["start", "wait", "send", "status", "stop", "resume"],
                   "description": "start: projeyi başlat | wait: proje bitene ya da kullanıcıya soru sorana kadar "
                   "bekle | send: yöneticiye (to=alice) ya da ajana mesaj | status: durum | stop: ajanı (name) ya "
                   "da projeyi durdur | resume: durmuş projeyi sürdür."},
        "goal": {"type": "string", "description": "start: işin genel hedefi ve bağlamı; herkes görür."},
        "agents": {"type": "array", "description": "start (isteğe bağlı): ajanlar (en çok 8). Boş: yönetici planlar.",
                   "items": AGENT_SPEC},
        "review": {"type": "boolean", "description": "start: biten işleri yönetici incelesin mi. Varsayılan true."},
        "council": {"type": "boolean", "description": "start: inceleme kurulu (plan, her iş ve final tartışılıp "
                    "oylanır). Varsayılan true; küçük ya da acil işte false."},
        "pr": {"type": "boolean", "description": "start: sonuç PR olsun. Proje kullanıcının kopyasına dokunmadan "
               "kendi dalında çalışır; bitince dal push'lanır, PR açılır ve takibe alınır (kırmızı CI ve yorumlar bu "
               "sohbete gelir). Kod deposunda, kullanıcı PR istediyse ya da iş incelenmesi gereken büyük bir "
               "değişiklikse kullan."},
        "project": {"type": "string", "description": "Proje kimliği. Boş: bu sohbette en son başlatılan."},
        "to": {"type": "string", "description": "send: 'alice' (yönetici), ajan adı ya da 'all'."},
        "text": {"type": "string", "description": "send: mesaj."},
        "name": {"type": "string", "description": "stop: tek ajan. Boş: proje."},
        "timeout": {"type": "integer", "description": "wait: en çok kaç saniye. Varsayılan 600, en çok 1800."},
    }, ["action"])
    risk = READ  # each tool call the agents make goes through its own permission check

    def summary(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a == "start":
            names = ", ".join(str(x.get("name")) for x in args.get("agents") or [] if isinstance(x, dict))
            return "proje: " + (names or "yönetici planlayacak") + " · " + str(args.get("goal", ""))[:80]
        if a == "send":
            return f"{args.get('to')}: {str(args.get('text', ''))[:120]}"
        return str(a)

    def run(self, ctx: ToolContext, action: str, goal: str = "", agents: list[dict[str, Any]] | None = None,
            review: bool = True, project: str = "", to: str = "", text: str = "", name: str = "",
            timeout: int = 600, council: bool = True, pr: bool = False) -> ToolResult:
        from alice.team import LEAD, Team

        teams = ctx.state.setdefault("teams", {})
        if action == "start":
            if not (goal or "").strip():
                raise ToolError("start için goal gerekli")
            specs = [a for a in agents or [] if isinstance(a, dict)]
            if len(specs) > 8:
                raise ToolError("en çok 8 ajan")
            a = ctx.config.agent
            team = Team(ctx, goal.strip(), max_parallel=int(a.get("team_max_parallel", 5)),
                        publish=ctx.state.get("publish"), review=review is not False, council=council is not False,
                        pr=bool(pr))
            for spec in specs:
                team.add(spec)
            ctx.state["team"] = team
            teams[team.id] = team
            team.start(plan=not specs)
            return ToolResult(f"proje başladı (kimlik {team.id}). "
                              + ("Yönetici kodu inceleyip ajanları kuracak. " if not specs else
                                 "Ajanlar:\n" + team.status_text() + "\n")
                              + "Kullanıcı Projeler sayfasından izleyebilir. Sonucu burada istiyorsa action=wait.")
        team = teams.get(project) if project else ctx.state.get("team")
        if team is None:
            return ToolResult("böyle bir proje yok; önce action=start", is_error=True)
        if action == "status":
            return ToolResult(f"proje {team.id}: {team.status()}\n{team.status_text()}"
                              + (f"\n\nÖzet:\n{team.summary}" if team.summary else ""))
        if action == "send":
            target = (to or LEAD).strip().lower()
            return ToolResult(team.send("kullanıcı", target, text))  # the ALICE in the chat, writing on the user's behalf
        if action == "stop":
            target = (name or "").strip().lower() or None
            if target and target not in team.members:
                raise ToolError(f"projede '{target}' yok")
            team.stop(target)
            return ToolResult(f"durduruldu: {target or 'proje'}")
        if action == "resume":
            team.resume()
            return ToolResult("proje sürdürülüyor")
        if action == "wait":
            limit = max(5, min(int(timeout or 600), 1800))
            team.wait_until(lambda: team.status() in ("done", "stopped", "needs_you"), limit, ctx.cancelled)
            st = team.status()
            if st == "done":
                return ToolResult("Proje bitti. Yöneticinin özeti:\n" + (team.summary or "(özet yok)"))
            if st == "needs_you":
                qs = [q["text"] for q in team.questions if not q.get("answer")]
                return ToolResult("Proje kullanıcıya soruyor:\n" + "\n".join(f"- {q}" for q in qs) +
                                  "\nKullanıcıya sor; cevabını action=send to=alice ile ilet.")
            return ToolResult(f"Proje durumu: {st}\n{team.status_text()}\n"
                              + ("Süre doldu; proje arka planda sürüyor." if st != "stopped" else ""))
        raise ToolError(f"bilinmeyen action: {action}")


class CouncilTool(Tool):
    name = "council"
    description = (
        "Bir konuyu inceleme kuruluna sorar: eleştirmen (acımasız ama kanıtlı), savunucu, kontrolcü (iddiaları "
        "kendisi doğrular) ve gerekince güvenlik/performans/kullanıcı uzmanları bağımsız inceler, tartışır ve "
        "ağırlıklı oylamayla karar verir. Fikir, tasarım kararı, dosya/kod, değişiklik ya da plan için.\n"
        "- Kullan: kullanıcı '/kurul', 'kurula sor', 'eleştirsinler', 'tartışsınlar' derse ya da önemli bir kararın "
        "artısını eksisini sağlam görmek gerekiyorsa. Dakikalar sürebilir.\n"
        "- Sonucu kullanıcıya özetle: karar, doğrulanmış bulgular, kim neyi neden savundu."
    )
    parameters = obj({
        "subject": {"type": "string", "description": "İncelenecek şey ve beklenti; konuşmayı görmeyen biri anlayacak "
                    "kadar net. Kod/dosya ise yollarını da yaz."},
        "paths": {"type": "array", "items": {"type": "string"}, "description": "Odaklanılacak dosyalar (isteğe bağlı)."},
        "critical": {"type": "boolean", "description": "Kritik konu (güvenlik, veri, ana mimari): tam kurul, en güçlü "
                     "modeller, derin tartışma."},
        "depth": {"type": "string", "enum": ["auto", "1", "2", "3"], "description": "Tartışma turu. auto: işe göre."},
    }, ["subject"])
    risk = READ

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("subject", ""))[:150]

    def run(self, ctx: ToolContext, subject: str, paths: list[str] | None = None, critical: bool = False,
            depth: str = "auto") -> ToolResult:
        if not (subject or "").strip():
            raise ToolError("subject boş")
        context = ""
        for p in (paths or [])[:8]:
            f = ctx.resolve(p)
            try:
                context += f"\n## {p}\n```\n{f.read_text(encoding='utf-8', errors='replace')[:8000]}\n```\n"
            except OSError:
                context += f"\n## {p}: okunamadı\n"
        from alice.council import Council, report

        ctx.emit("kurul toplanıyor…")
        rules = ""
        for f in (ctx.cwd / "REVIEW.md", ctx.cwd / ".alice" / "REVIEW.md"):
            if f.is_file():
                rules = f.read_text(encoding="utf-8", errors="replace")[:4000]
                break
        c = Council(ctx, "adhoc", subject, context, critical=bool(critical), depth=depth if depth != "auto" else "auto",
                    rules=rules)
        snap = c.run()
        ctx.emit("kurul kararını verdi")
        return ToolResult(report(snap))


TOOLS = [RunPlan(), TaskControl(), TeamTool(), CouncilTool()]
