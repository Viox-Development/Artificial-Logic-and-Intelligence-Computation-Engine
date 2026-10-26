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
        "Ekranda/tarayıcıda bir hedefi Jev'le hızlıca yapar (adım ~1 sn): ekranı okur, her adımda eylemi seçer: "
        "tıklama, sağ/çift tık, alana yazma, pencereye klavyeyle yazma, tuş ya da kombinasyon (ctrl+shift+s), tuşu "
        "basılı tutma, sürükle-bırak, kaydırma. Sonuçta ekranın başa göre değişen yazıları da gelir.\n"
        "- Kullan: 'Impossible şarkısına tıkla', 'Ayarlar > Gizlilik'i aç', 'kartı sağdaki sütuna sürükle'.\n"
        "- Klavye işi tek adımdır: hesap makinesinde tuşlara tıklatma, text='1234*5678=' ver; gereken "
        "kombinasyonları keys'e yaz.\n"
        "- Uygulama açmaz: önce app/windows aracıyla aç, sonra title ver (boşsa öndeki pencere).\n"
        "- Büyük işlerde sen planla: işi kısa hedeflere böl, her biri için reflex çağır; oyun kuralları, strateji "
        "ya da ekranda neyin ne olduğunu notes'a yaz (İngilizce daha iyi).\n"
        "- Sonuç 'needs_help' ise nedenine bak; hedefi değiştir, notes'a bilgi ekle ya da kalanı kendin yap.\n"
        "- TypeSafe anahtarı varsa listede olmayan şeyleri (ikon, oyun taşı) ekran görüntüsünden de bulabilir."
    )
    parameters = obj({
        "goal": {"type": "string", "description": "Kısa ve somut hedef, ekranda göründüğü adlarla, ör. 'ekrandaki "
                 "Impossible şarkısına tıkla'."},
        "target": {"type": "string", "enum": ["browser", "desktop"],
                   "description": "browser: kullanıcının tarayıcısında öndeki sekme | desktop: masaüstü penceresi."},
        "url": {"type": "string", "description": "browser için: önce bu adrese git. Boş: öndeki sekmede kal."},
        "title": {"type": "string", "description": "desktop için pencere başlığından bir parça. Boş: öndeki pencere."},
        "inputs": {"type": "object", "additionalProperties": {"type": "string"},
                   "description": "Doldurulacak alanlar: alan adından parça -> yazılacak metin, ör. {\"ara\": \"lofi\"}."},
        "notes": {"type": "string", "description": "Karar motoruna bağlam: kurallar, strateji, ekranda neyin ne "
                  "olduğu, kaçınılacaklar (kısa, tercihen İngilizce)."},
        "text": {"type": "string", "description": "Pencereye klavyeyle yazılacak metin (alan seçmeden), ör. "
                 "'1234*5678='."},
        "keys": {"type": "array", "items": {"type": "string", "description": "Tuş ya da kombinasyon, ör. 'shift+w'."},
                 "description": "Seçeneklere eklenecek tuşlar/kombinasyonlar (aynı anda basılır)."},
        "hold_seconds": {"type": "number", "description": "Tuş basılı tutulacaksa kaç saniye. Varsayılan 1."},
        "max_steps": {"type": "integer", "description": "En fazla adım. Varsayılan 20."},
    }, ["goal", "target"])
    risk = COMPUTER

    def run(self, ctx: ToolContext, goal: str, target: str, url: str | None = None, title: str | None = None,
            inputs: dict[str, str] | None = None, notes: str = "", max_steps: int = 20, text: str = "",
            keys: list[str] | None = None, hold_seconds: float | None = None) -> ToolResult:
        from alice.config import alice_home
        from alice.reflex.agent import ScreenAgent, make_translator
        from alice.reflex.loop import make_text_writer
        from alice.reflex.observers import BrowserObserver, DesktopObserver

        if ctx.decider is None or ctx.router is None:
            raise ToolError("karar motoru yapılandırılmamış")
        if target == "browser":
            observer = BrowserObserver(ctx, url)
        else:
            observer = DesktopObserver(ctx, title, goal, echoes=_last_user_text(ctx))
        loop = ScreenAgent(observer, ctx.decider, make_text_writer(ctx.router), make_translator(ctx.router),
                           emit=ctx.emit, trace_path=alice_home() / "reflex-trace.jsonl")
        if hold_seconds:
            observer.hold_seconds = max(0.05, min(float(hold_seconds), 10.0))
        extra = {k: v for k, v in (("text", text), ("keys", keys)) if v}
        res = loop.run(goal, max_steps=max_steps, inputs=inputs, notes=notes or "", **extra)
        verdict = reflex_verdict(getattr(res, "status", ""))
        return ToolResult(res.report() + ("\n" + verdict if verdict else ""),
                          is_error=getattr(res, "status", "") == "error")


def reflex_verdict(status: str) -> str:
    """Durumu açıkça söyle: modeller "Jev cevap verdi" ile "hedef yapıldı"yı karıştırıp kullanıcıya "çalışıyor,
    başardı" diyordu (needs_help sonucunda bile)."""
    return {"done": "SONUÇ: hedef yapıldı.",
            "clicked": "SONUÇ: hedef sanılan yere tıklandı ama sonucu doğrulanmadı. Bir ekran görüntüsüyle doğru "
                       "şeyin açıldığına bak; doğruysa bitti, değilse kendin düzelt.",
            "likely_done": "SONUÇ: son eylem ekranı değiştirdi ve karar motoru hedefin büyük ihtimalle yapıldığını "
                           "düşünüyor ama emin değil. Bir ekran görüntüsüyle (ya da dosyaya bakarak) doğrula, sonra "
                           "kullanıcıya söyle.",
            "needs_help": "SONUÇ: hedef YAPILAMADI (karar motoru cevap verdi ama yol bulamadı). Kullanıcıya başardı "
                          "deme; kalanını kendin yap ya da nedenini söyle.",
            "acted": "SONUÇ: bir eylem yapıldı ve ekran değişti; istenenin olup olmadığı ayrıca doğrulanmalı.",
            "max_steps": "SONUÇ: adım sınırı doldu, hedef YAPILAMADI.",
            "error": "SONUÇ: hata, hedef YAPILAMADI."}.get(status, "")


def _last_user_text(ctx: ToolContext) -> tuple[str, ...]:
    """Kullanıcının son mesajı: sohbet penceresinde görünür; reflex onu ekrandaki bir hedef sanmasın."""
    for m in reversed(getattr((getattr(ctx, "state", None) or {}).get("agent"), "messages", None) or []):
        if m.get("role") != "user" or m.get("_nudge"):
            continue
        # İçeriğe hafızadan hatırlananlar eklenir (proje adları dahil): onlar da "yankı" sayılınca kenar çubuğundaki
        # projeler listeden düşüyordu. Kullanıcının yazdığı _display'de.
        text = m.get("_display") or m.get("content")
        if isinstance(text, list):
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
        return (text,) if isinstance(text, str) and text else ()
    return ()


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
