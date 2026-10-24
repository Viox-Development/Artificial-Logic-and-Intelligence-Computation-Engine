"""Ajanın kendini yönetmesi için araçlar: todo listesi, kalıcı hafıza, alt ajan, reflex."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class TodoWrite(Tool):
    name = "todo_write"
    description = ("Çok adımlı (3+ adım) bir işte planı ve ilerlemeyi listede tutar; kullanıcı da listeyi görür. "
                   "Her çağrıda listenin TAMAMINI gönder; aynı anda tek bir in_progress olsun, bir adım biter bitmez "
                   "done yap. Tek adımlık ya da sohbet işlerinde kullanma.")
    parameters = obj({"todos": {"type": "array", "description": "Güncel listenin tamamı.", "items": obj({
        "content": {"type": "string", "description": "Adım, emir kipinde kısa cümle, ör. 'Testleri çalıştır'."},
        "status": {"type": "string", "enum": ["pending", "in_progress", "done"],
                   "description": "pending: başlamadı | in_progress: şu an yapılan | done: bitti."}},
        ["content", "status"])}}, ["todos"])

    def summary(self, args: dict[str, Any]) -> str:
        todos = args.get("todos") or []
        done = sum(1 for t in todos if t.get("status") == "done")
        return f"{len(todos)} görev, {done} tamam"

    def run(self, ctx: ToolContext, todos: list[dict[str, Any]]) -> ToolResult:
        ctx.state["todos"] = todos
        mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
        text = "\n".join(f"{mark.get(t.get('status'), '[ ]')} {t.get('content')}" for t in todos)
        return ToolResult(text or "liste boş")


class Memory(Tool):
    name = "memory"
    description = ("Oturumlar arası kalıcı hafıza. Kullanıcı hakkında ileride işe yarayacak kalıcı bilgiyi kaydet: "
                   "tercihleri, kişisel bilgileri, çevresindeki kişiler, alışkanlıkları, ALICE'ten nasıl davranmasını "
                   "istediği ('bunu hatırla', 'bundan sonra hep...').\n"
                   "- Bilgi değişince yenisini eklemek yerine eskisini update ile güncelle; yanlışsa forget.\n"
                   "- Geçmiş konuşmalarda ya da kayıtlarda bir şey aramak için search ('geçen hafta ne konuşmuştuk').\n"
                   "- Takip: ileride sorulacak/hatırlatılacak bir şey için followup (ör. sınav yarın: due=sınavdan sonraki "
                   "gün, 'Sınavın nasıl geçtiğini sor'); profilde 'Follow-ups due' altındakini sorunca done.\n"
                   "- Profil zaten sistem isteminde; sadece okumak için çağırma. Geçici şeyleri (o anki iş) kaydetme; "
                   "not için notes aracı.")
    parameters = obj({
        "action": {"type": "string", "enum": ["read", "add", "update", "forget", "search", "followup", "followups",
                                              "done"],
                   "description": "read: hepsini listele | add: note'u kaydet | update: id'li bilgiyi note ile değiştir "
                   "| forget: id'li ya da query'ye uyan bilgiyi sil | search: query ile ara | followup: note'u takip "
                   "olarak ekle (due, kind) | followups: açık takipleri listele | done: id'li takibi kapat."},
        "note": {"type": "string", "description": "add/update için tek, kısa Türkçe cümle, ör. 'Kullanıcı kahveyi "
                 "şekersiz içer.'"},
        "kind": {"type": "string", "enum": ["preference", "fact", "person", "routine", "note"],
                 "description": "add için tür: preference tercih, fact bilgi, person kişi, routine alışkanlık. "
                 "Varsayılan fact."},
        "id": {"type": "integer", "description": "update/forget için bilginin numarası (read/search çıktısındaki #)."},
        "query": {"type": "string", "description": "search ya da forget için arama ifadesi."},
        "due": {"type": "string", "description": "followup için gün, YYYY-MM-DD. kind=open ise boş."},
        "followup_kind": {"type": "string", "enum": ["ask", "remind", "open"],
                          "description": "followup türü: ask sonra sor | remind o gün hatırlat | open yarım kalan iş."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        return WRITE if args.get("action") == "forget" else READ

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('note') or args.get('query') or args.get('id') or ''}".strip()

    def run(self, ctx: ToolContext, action: str, note: str | None = None, kind: str | None = None,
            id: int | None = None, query: str | None = None, text: str | None = None, due: str | None = None,
            followup_kind: str | None = None) -> ToolResult:
        from alice.memory import Fact, _parse_day, shared_memory

        mem = ctx.state.get("memory") or shared_memory(ctx.config, ctx.router)
        if mem is None:
            raise ToolError("hafıza kapalı (config.toml [memory] enabled = false)")
        note = note or text
        if action == "read":
            return ToolResult(mem.export_markdown())
        if action == "search":
            if not (query or note):
                raise ToolError("search için query gerekli")
            hits = mem.search(query or note or "", limit=8, min_score=0.2)
            return ToolResult("\n".join(h.line() for h in hits) or "hafızada ilgili bir şey yok")
        if action == "add":
            if not note:
                raise ToolError("add için note gerekli")
            fid, new = mem.remember(note, kind or "fact", source="araç")
            return ToolResult(f"kaydedildi #{fid}" if new else f"benzer bilgi güncellendi #{fid}")
        if action == "update":
            if id is None or not note:
                raise ToolError("update için id ve note gerekli")
            return ToolResult(f"güncellendi #{id}" if mem.update(id, note) else f"#{id} bulunamadı")
        if action == "forget":
            if id is not None:
                gone = mem.forget(id)
                return ToolResult(f"silindi: {gone.text}" if gone else f"#{id} bulunamadı")
            if not (query or note):
                raise ToolError("forget için id ya da query gerekli")
            gone, hits = mem.forget_matching(query or note or "")
            if gone:
                return ToolResult(f"silindi: {gone[0].text}")
            cands = [h.item.line() for h in hits if isinstance(h.item, Fact)]
            return ToolResult("emin olamadım, hiçbirini silmedim. Adaylar (id ile sil):\n" + "\n".join(cands)
                              if cands else "eşleşen bilgi yok")
        if action == "followup":
            if not note:
                raise ToolError("followup için note gerekli")
            kind = followup_kind or ("open" if not due else "ask")
            day = _parse_day(due)
            if kind != "open" and day is None:
                raise ToolError("followup için due (YYYY-MM-DD) gerekli; yarım kalan iş ise followup_kind=open")
            return ToolResult(f"takip eklendi #{mem.add_followup(note, day, kind)}")
        if action == "followups":
            return ToolResult("\n".join(f.line() for f in mem.followups()) or "açık takip yok")
        if action == "done":
            if id is None:
                raise ToolError("done için id gerekli")
            f = mem.done_followup(id)
            return ToolResult(f"kapatıldı: {f.text}" if f else f"#{id} bulunamadı")
        raise ToolError(f"bilinmeyen action: {action}")


class Task(Tool):
    name = "task"
    description = ("Kendi temiz bağlamıyla çalışan bir alt ajan başlatır; bağımsız bir işi (araştırma, büyük bir "
                   "klasörde arama, uzun bir alt görev) yapar ve sadece sonucunu döndürür. Konuşmayı çok sayıda ara "
                   "çıktıyla doldurmamak için. Alt ajan bu konuşmayı görmez: prompt'a gereken her şeyi yaz. Bir iki "
                   "araç çağrısıyla bitecek işlerde kullanma.")
    parameters = obj({
        "prompt": {"type": "string", "description": "Alt ajana eksiksiz görev: ne yapacak, nerede, ne döndürecek."},
        "agent": {"type": "string", "description": "Kullanıcının tanımladığı özel alt ajanın adı (sistem istemindeki "
                  "'Custom subagents' listesi). Boş: genel alt ajan."},
        "max_steps": {"type": "integer", "description": "En fazla adım. Varsayılan 40 (özel ajanda kendi sınırı)."},
    }, ["prompt"])
    risk = READ  # alt ajanın kendi araç çağrıları ayrı ayrı izin kontrolünden geçer

    def run(self, ctx: ToolContext, prompt: str, max_steps: int | None = None, agent: str = "") -> ToolResult:
        parent = ctx.state.get("agent")
        if parent is None:
            raise ToolError("alt ajan bu bağlamda kullanılamıyor")
        if agent:
            from alice.skills import load_agents, spawn

            defs = load_agents(ctx.cwd)
            adef = defs.get(agent.strip().lower())
            if adef is None:
                raise ToolError(f"'{agent}' adında alt ajan yok. Tanımlı olanlar: {', '.join(defs) or '(yok)'}")
            sub = spawn(parent, adef)
            steps = max_steps or adef.max_steps
        else:
            sub = parent.spawn_child()
            steps = max_steps or 40
        ctx.emit(f"alt ajan başladı{f' ({agent})' if agent else ''}")
        result = sub.run_turn(prompt, max_steps=steps)
        ctx.emit("alt ajan bitti")
        return ToolResult(result or "(alt ajan boş cevap döndürdü)")


class Reflex(Tool):
    name = "reflex"
    description = (
        "Ekranda görünen bir şeye tıklama, bir alana yazma gibi kısa ve net bir arayüz hedefini hızlıca yapar "
        "(adım başına ~0,1-0,5 sn). Sayfayı öğe listesiyle okur; her sitede ve uygulamada aynı çalışır.\n"
        "- Kullan: 'ekrandaki Impossible şarkısına tıkla', 'Ayarlar > Gizlilik'i aç', 'arama kutusuna X yazıp ilk "
        "sonuca tıkla'.\n"
        "- Kullanma: birçok adımlı, ekranı izleyerek yapılacak işler (computer_task).\n"
        "- Sonuç 'needs_help' ise kalan kısmı browser snapshot/click ya da ui_elements/ui_act ile sen bitir."
    )
    parameters = obj({
        "goal": {"type": "string", "description": "Hedef, ekranda göründüğü adlarla, ör. 'ekrandaki Impossible "
                 "şarkısına tıkla'."},
        "target": {"type": "string", "enum": ["browser", "desktop"],
                   "description": "browser: kullanıcının tarayıcısında öndeki sekme | desktop: masaüstü penceresi."},
        "url": {"type": "string", "description": "browser için: önce bu adrese git. Boş: öndeki sekmede kal."},
        "title": {"type": "string", "description": "desktop için pencere başlığından bir parça. Boş: öndeki pencere."},
        "inputs": {"type": "object", "additionalProperties": {"type": "string"},
                   "description": "Doldurulacak alanlar: alan adından parça -> yazılacak metin, ör. {\"ara\": \"lofi\"}."},
        "max_steps": {"type": "integer", "description": "En fazla adım. Varsayılan 20."},
    }, ["goal", "target"])
    risk = COMPUTER

    def run(self, ctx: ToolContext, goal: str, target: str, url: str | None = None, title: str | None = None,
            inputs: dict[str, str] | None = None, max_steps: int = 20) -> ToolResult:
        from alice.reflex.loop import ReflexLoop, make_text_writer
        from alice.reflex.observers import BrowserObserver, DesktopObserver

        if ctx.decider is None or ctx.router is None:
            raise ToolError("karar motoru yapılandırılmamış")
        observer = BrowserObserver(ctx, url) if target == "browser" else DesktopObserver(ctx, title)
        loop = ReflexLoop(observer, ctx.decider, make_text_writer(ctx.router),
                          min_confidence=float(ctx.config.decider.get("min_confidence", 0.55)), emit=ctx.emit)
        return ToolResult(loop.run(goal, max_steps=max_steps, inputs=inputs).report())


class AskUser(Tool):
    name = "ask_user"
    description = ("Kullanıcıya tıklanabilir seçeneklerle soru sorar ve cevabını bekler (uygulamada kart, terminalde "
                   "numaralı liste). Sadece gerçekten kullanıcının vermesi gereken bir kararda kullan (iki makul yol, "
                   "eksik bilgi); varsayılanı belli işlerde sorma. Önerdiğin seçeneği ilk sıraya koy. Kullanıcı "
                   "listede olmayan bir cevap da yazabilir.")
    parameters = obj({
        "question": {"type": "string", "description": "Tek, net soru."},
        "options": {"type": "array", "items": {"type": "string", "description": "Seçenek: kısa etiket, istersen "
                    "' — ' ile açıklama."}, "description": "2-5 seçenek."},
        "multi": {"type": "boolean", "description": "Birden fazla seçilebilsin mi? Varsayılan false."},
    }, ["question", "options"])
    risk = READ

    def run(self, ctx: ToolContext, question: str, options: list[str], multi: bool = False) -> ToolResult:
        ui = getattr(ctx.state.get("agent"), "ui", None)
        # Sınıfta tanımlı olmalı: alt ajan/ses arayüzlerinin __getattr__'ı her adı sessizce yutan bir fonksiyon döndürür.
        ask = getattr(ui, "ask_choice", None) if callable(getattr(type(ui), "ask_choice", None)) else None
        opts = [str(o).strip() for o in options or [] if str(o).strip()][:6]
        if len(opts) < 2:
            raise ToolError("en az iki seçenek gerekli")
        if not callable(ask):
            return ToolResult("Bu arayüzde seçenekli soru gösterilemiyor (sesli konuşma ya da gözetimsiz çalışma). "
                              "Soruyu ve seçenekleri cevabında kısaca sor, sonra dur ve kullanıcının cevabını bekle.")
        answer = (ask(question, opts, bool(multi)) or "").strip()
        if not answer:
            return ToolResult("Kullanıcı cevap vermedi ya da soruyu kapattı. Varsayımda bulunma; kısaca sor ya da dur.")
        return ToolResult(f"Kullanıcının cevabı: {answer}")


class SkillTool(Tool):
    name = "skill"
    description = ("Kullanıcının ALICE'e öğrettiği bir skill'i (talimat + dosyalar) yükler. Sistem istemindeki "
                   "'Skills' listesindeki bir iş geldiğinde önce bunu çağır, sonra yazdığına göre çalış.")
    parameters = obj({"name": {"type": "string", "description": "Skill adı (listede yazan)."}}, ["name"])
    risk = READ
    parallel_safe = True

    def run(self, ctx: ToolContext, name: str) -> ToolResult:
        from alice.skills import load_skills

        skills = load_skills(ctx.cwd)
        sk = skills.get(name.strip().lower())
        if sk is None:
            raise ToolError(f"'{name}' adında skill yok. Olanlar: {', '.join(skills) or '(yok)'}")
        files = sk.files()
        listing = ("\nKlasördeki dosyalar (yollar bu klasöre göre):\n" + "\n".join(f"- {f}" for f in files)) if files else ""
        return ToolResult(f"# Skill: {sk.name}\nKlasör: {sk.path.parent}\n\n{sk.body()}{listing}")


class Projects(Tool):
    name = "projects"
    description = ("Kullanıcının bilgisayarındaki projeler (git depoları, Unity, Python, Node, .NET...): listele, ada/"
                   "konuya göre bul, bir projenin ayrıntısı (yol, diller, son commitler, klasör içeriği, README). "
                   "'Unity projem', 'oyunum', 'ALICE reposu' gibi sözlerde hangi klasör olduğunu buradan öğren.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "find", "info", "rescan"],
                   "description": "list: en son çalışılanlar | find: query'ye uyanlar | info: query'deki projenin "
                   "ayrıntısı | rescan: diski yeniden tara (yeni proje eklendiyse)."},
        "query": {"type": "string", "description": "find/info için proje adı ya da konu, ör. 'fps', 'alice', 'unity'."},
    }, ["action"])
    risk = READ

    def run(self, ctx: ToolContext, action: str, query: str = "") -> ToolResult:
        from alice import projects as pj

        if action == "rescan":
            found = pj.refresh(ctx.config)
            return ToolResult(f"{len(found)} proje bulundu:\n" + "\n".join(f"- {p.line()}" for p in found[:15]))
        found = pj.get(ctx.config, background=False)
        if not found:
            return ToolResult("proje bulunamadı. Proje klasörünü config.toml [projects] paths'e ekleyip rescan yap.")
        if action == "list":
            return ToolResult("\n".join(f"- {p.line()}" for p in found[:25]))
        if not query:
            raise ToolError(f"{action} için query gerekli")
        hits = pj.find(query, found)
        if not hits:
            return ToolResult(f"'{query}' ile eşleşen proje yok. En son çalışılanlar:\n"
                              + "\n".join(f"- {p.line()}" for p in found[:8]))
        if action == "info":
            return ToolResult(pj.info(hits[0]) + ("\nDiğer adaylar: " + ", ".join(p.name for p in hits[1:5])
                                                 if len(hits) > 1 else ""))
        return ToolResult("\n".join(f"- {p.line()}" for p in hits[:10]))


class Feedback(Tool):
    name = "feedback"
    description = ("Kullanıcı az önceki bir cevabını beğenmediğini söylerse ('bu cevap kötüydü', 'saçmaladın', 'öyle deme', "
                   "'yanlış anladın') çağır: istek ve cevap test setine kaydedilir, sonraki güncellemelerde denenir. "
                   "Kullanıcıya kısaca kabul ettiğini söyle (özür kalıbı yok); ne yapmamanı istiyorsa memory'ye de "
                   "tercih olarak ekle.")
    parameters = obj({
        "say": {"type": "string", "description": "Beğenilmeyen cevaptan önce kullanıcının söylediği istek, kendi sözleriyle."},
        "reply": {"type": "string", "description": "Senin verdiğin, beğenilmeyen cevap."},
        "note": {"type": "string", "description": "Kullanıcının ne dediği / neyin yanlış olduğu."},
    }, ["note"])
    risk = READ

    def run(self, ctx: ToolContext, note: str, say: str = "", reply: str = "") -> ToolResult:
        from alice.evals import record_feedback

        new = record_feedback(say, reply, note, source="sohbet")
        return ToolResult("kaydedildi" + (", test setine eklendi" if new else ""))


TOOLS = [TodoWrite(), Memory(), Task(), Reflex(), Projects(), SkillTool(), AskUser(), Feedback()]
