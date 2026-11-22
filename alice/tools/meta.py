"""Tools for the agent to manage itself: todo list, persistent memory, sub-agent, reflex."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class TodoWrite(Tool):
    name = "todo_write"
    description = ("Çok adımlı bir işte planı ve ilerlemeyi yapılacaklar listesinde tutar; kullanıcı listeyi canlı görür.\n"
                   "- 3 ya da daha fazla adımlı işlerde, kullanıcı birden çok şey istediğinde ya da iş uzun sürecekse kullan. "
                   "Tek adımlık işlerde ve sohbette kullanma.\n"
                   "- Her çağrıda listenin TAMAMINI gönder. Aynı anda tek bir in_progress olsun; bir adım biter bitmez done yap, "
                   "birkaçını sona biriktirme.\n"
                   "- Bir adım gerçekten bitmediyse (test geçmedi, hata kaldı) done yapma; engeli yeni bir adım olarak ekle.")
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
    description = ("Kalıcı hafıza: kullanıcıya ve işlerine dair NOTLAR (her biri ayrı dosya; kurallar ve MEMORY.md "
                   "dizini sistem isteminde) ve geçmiş konuşmalar.\n"
                   "- save: not yaz ya da aynı adla güncelle (name, type, description, content; scope=proje yalnız bu "
                   "depoya özgü olanlar). Benzer not varsa onu güncelle. 'Hatırla' denince hemen save, 'unut' "
                   "denince delete.\n"
                   "- read: name ile notun tamamı (dizindeki bir not işe yarayacaksa oku); name yoksa hepsi.\n"
                   "- Kullanıcı geçmişe değinince ('geçen sefer', 'dün konuştuğumuz') önce bak: search_chats (konu), "
                   "recent_chats (zaman, after/before). Bulamazsan uydurma.\n"
                   "- log: uzun işte not etmeye değeri bugünün günlüğüne ekle. dream: hafızayı şimdi toparla. "
                   "restore: Dream yedeğine dön.\n"
                   "- followup: ileride sorulacak/hatırlatılacak şey (due); sorunca done.")
    parameters = obj({
        "action": {"type": "string", "enum": ["save", "read", "delete", "list", "search", "log", "dream", "restore",
                                              "search_chats", "recent_chats", "followup", "followups", "done",
                                              "add", "update", "forget"],
                   "description": "save | read | delete | list (notlar) | search (notlarda ara) | log (günlüğe ekle) | "
                   "dream (toparla) | restore (yedeğe dön; backup boşsa yedekleri listeler) | search_chats | "
                   "recent_chats | followup | followups | done. add/update/forget eski adlar (save/delete)."},
        "name": {"type": "string", "description": "Notun kısa adı (kebab-case, ör. 'feedback-testler'); read/delete/"
                 "save için. Başka kapsamdaki not: 'proje:ad'."},
        "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"], "description": "save: tür."},
        "description": {"type": "string", "description": "save: tek satır, belirgin açıklama (hatırlamada bu satıra "
                        "bakılarak seçilir)."},
        "content": {"type": "string", "description": "save: notun gövdesi (markdown). feedback/project için: kural ya "
                    "da bilgi, sonra '**Neden:**' ve '**Nasıl uygulanır:**' satırları. Göreli tarihleri mutlak yaz."},
        "title": {"type": "string", "description": "save: dizinde görünecek kısa başlık (boşsa ad)."},
        "scope": {"type": "string", "enum": ["genel", "proje"], "description": "save: genel (varsayılan) ya da "
                  "proje (yalnız şu anki git deposunda geçerli)."},
        "note": {"type": "string", "description": "log/followup için metin; add için tek cümle."},
        "kind": {"type": "string", "description": "add için eski tür adı (preference, fact, person...)."},
        "id": {"type": "integer", "description": "done için takibin numarası."},
        "query": {"type": "string", "description": "search, search_chats ya da forget için arama ifadesi."},
        "backup": {"type": "string", "description": "restore: dönülecek yedek (YYYYMMDD-HHMMSS)."},
        "due": {"type": "string", "description": "followup için gün, YYYY-MM-DD. kind=open ise boş."},
        "followup_kind": {"type": "string", "enum": ["ask", "remind", "open"],
                          "description": "followup türü: ask sonra sor | remind o gün hatırlat | open yarım kalan iş."},
        "n": {"type": "integer", "description": "recent_chats için kaç konuşma (varsayılan 5, en çok 20)."},
        "after": {"type": "string", "description": "search_chats/recent_chats: bu günden (YYYY-MM-DD, dahil) sonrası."},
        "before": {"type": "string", "description": "search_chats/recent_chats: bu günden (YYYY-MM-DD) öncesi."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        # Writing a note is memory's normal job (Claude Code doesn't ask either); deleting or restoring a backup
        # can be undone, but the user's information may be lost, so it asks.
        return WRITE if args.get("action") in ("delete", "forget", "restore") else READ

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('name') or args.get('note') or args.get('query') or args.get('id') or ''}".strip()

    def run(self, ctx: ToolContext, action: str, name: str | None = None, type: str | None = None,  # noqa: A002
            description: str | None = None, content: str | None = None, title: str | None = None,
            scope: str | None = None, note: str | None = None, kind: str | None = None, id: int | str | None = None,  # noqa: A002
            query: str | None = None, backup: str | None = None, text: str | None = None, due: str | None = None,
            followup_kind: str | None = None, n: int | None = None, after: str | None = None,
            before: str | None = None) -> ToolResult:
        from alice.memdir import MemoryFull
        from alice.memory import _day_start, _parse_day, shared_memory

        mem = ctx.state.get("memory") or shared_memory(ctx.config, ctx.router)
        if mem is None:
            raise ToolError("hafıza kapalı (config.toml [memory] enabled = false)")
        note = note or text
        cwd = ctx.cwd
        if action in ("save", "add", "update"):
            ctx.state["memory_wrote"] = True
            try:
                if action == "save" or content:
                    if not (content or description):
                        raise ToolError("save için content (ve description) gerekli")
                    if not type:
                        raise ToolError("save için type gerekli: user, feedback, project ya da reference")
                    rec, new = mem.save_note(name or title or description or "", description or "", type,
                                             content or description or "", title=title or "", source="araç",
                                             scope=scope or "genel", cwd=cwd)
                    return ToolResult(f"{'kaydedildi' if new else 'güncellendi'}: {rec.key} ({rec.path})")
                if action == "update" and (name or id is not None):
                    return ToolResult("güncellendi" if mem.update(str(name or id), note or "", cwd) else "not yok")
                if not note:
                    raise ToolError("add için note gerekli")
                key, new = mem.remember(note, kind or "fact", source="araç", cwd=cwd, scope=scope or "genel")
                return ToolResult(f"kaydedildi: {key}" if new else f"benzer not güncellendi: {key}")
            except MemoryFull as e:
                raise ToolError(str(e)) from None
            except ValueError as e:
                raise ToolError(str(e)) from None
        if action in ("delete", "forget"):
            ctx.state["memory_wrote"] = True
            if name or (id is not None and not str(id).isdigit()):
                gone = mem.delete_note(str(name or id), cwd)
                return ToolResult(f"silindi (çöpe): {gone.key}" if gone else f"not yok: {name or id}")
            if not (query or note):
                raise ToolError("delete için name ya da query gerekli")
            gone, hits = mem.forget_matching(query or note or "", cwd)
            if gone:
                return ToolResult(f"silindi (çöpe): {gone[0].key}")
            return ToolResult("emin olamadım, hiçbirini silmedim. Adaylar (name ile sil):\n"
                              + "\n".join(h.manifest_line() for h in hits) if hits else "eşleşen not yok")
        if action == "read":
            if name:
                _, rec = mem.find_note(name, cwd)
                if rec is None:
                    raise ToolError(f"not yok: {name} (memory action=list ile bak)")
                return ToolResult(f"{rec.render(limit=8000)}\n\n(dosya: {rec.path})")
            return ToolResult(mem.export_markdown(cwd))
        if action == "list":
            lines = []
            for b in mem.books(cwd):
                notes = b.scan()
                lines.append(f"## {b.scope} ({b.root}) — {len(notes)} not")
                lines += [x.manifest_line() for x in notes] or ["(boş)"]
            return ToolResult("\n".join(lines))
        if action == "search":
            if not (query or note):
                raise ToolError("search için query gerekli")
            hits = mem.search_notes(query or note or "", cwd)
            return ToolResult("\n".join(h.manifest_line() for h in hits) or "notlarda ilgili bir şey yok "
                              "(geçmiş konuşmalar için search_chats)")
        if action == "log":
            if not note:
                raise ToolError("log için note gerekli")
            p = mem.book(scope or "genel", cwd).append_log(note)
            return ToolResult(f"günlüğe eklendi: {p}")
        if action == "dream":
            done = mem.dream(force=True, cwd=cwd)
            ctx.state["memory_wrote"] = True
            return ToolResult("Dream bitti:\n" + ("\n".join(f"- {x}" for x in done) or "- değişiklik yok"))
        if action == "restore":
            b = mem.book(scope or "genel", cwd)
            if not backup:
                return ToolResult("yedekler (yeniden eskiye): " + (", ".join(b.backups()) or "yok"))
            try:
                count = b.restore(backup)
            except ValueError as e:
                raise ToolError(str(e)) from None
            mem._bump()
            return ToolResult(f"{backup} yedeğine dönüldü ({count} not); dönmeden önceki hâl de yedeklendi")
        if action == "search_chats":
            if not (query or note):
                raise ToolError("search_chats için query gerekli")
            return ToolResult(mem.search_chats(query or note or "", after=_day_start(after), before=_day_start(before)))
        if action == "recent_chats":
            return ToolResult(mem.recent_chats(n or 5, after=_day_start(after), before=_day_start(before)))
        if action == "followup":
            if not note:
                raise ToolError("followup için note gerekli")
            fk = followup_kind or ("open" if not due else "ask")
            day = _parse_day(due)
            if fk != "open" and day is None:
                raise ToolError("followup için due (YYYY-MM-DD) gerekli; yarım kalan iş ise followup_kind=open")
            return ToolResult(f"takip eklendi #{mem.add_followup(note, day, fk)}")
        if action == "followups":
            return ToolResult("\n".join(f.line() for f in mem.followups()) or "açık takip yok")
        if action == "done":
            if id is None:
                raise ToolError("done için id gerekli")
            f = mem.done_followup(int(id))
            return ToolResult(f"kapatıldı: {f.text}" if f else f"#{id} bulunamadı")
        raise ToolError(f"bilinmeyen action: {action}")


class Task(Tool):
    name = "task"
    description = ("Kendi temiz bağlamıyla çalışan bir alt ajan başlatır; bağımsız bir işi (araştırma, büyük bir "
                   "klasörde arama, uzun bir alt görev) yapar ve sadece sonucunu döndürür. Konuşmayı çok sayıda ara "
                   "çıktıyla doldurmamak için. Alt ajan bu konuşmayı görmez: prompt'a gereken her şeyi yaz. Bir iki "
                   "araç çağrısıyla bitecek işlerde kullanma.\n"
                   "- Paralel: birbirinden bağımsız işler için aynı cevapta birden çok task çağır; aynı anda çalışırlar.\n"
                   "- background=true: alt ajan arka planda çalışır, sen ve kullanıcı sohbete devam edersiniz; bitince "
                   "sonucu '[Alt ajan aN ...]' mesajıyla gelir. Sonuca hemen ihtiyacın yoksa ya da iş uzunsa kullan. "
                   "Sonucu gelmeden ne bulduğunu tahmin etme.\n"
                   "- id: daha önce çalışmış alt ajana (aN) yeni mesaj; okuduklarını ve bulduklarını hatırlar, baştan "
                   "araştırmaz. Aynı konuda ek soru için yeni alt ajan yerine bunu kullan.\n"
                   "- Durum ve durdurma: subagents aracı (list/stop). Kullanıcı yan paneldeki Ajanlar sekmesinden izler.")
    parameters = obj({
        "prompt": {"type": "string", "description": "Alt ajana eksiksiz görev: ne yapacak, nerede, ne döndürecek. "
                   "id verildiyse: ona yazılacak yeni mesaj."},
        "agent": {"type": "string", "description": "Kullanıcının tanımladığı özel alt ajanın adı (sistem istemindeki "
                  "'Custom subagents' listesi). Boş: genel alt ajan."},
        "name": {"type": "string", "description": "Ekranda görünecek kısa ad, ör. 'test taraması'."},
        "background": {"type": "boolean", "description": "Arka planda çalıştır; sonuç bitince mesaj olarak gelir."},
        "id": {"type": "string", "description": "Devam ettirilecek alt ajan (aN). Boş: yeni alt ajan."},
        "max_steps": {"type": "integer", "description": "En fazla adım. Varsayılan 40 (özel ajanda kendi sınırı)."},
    }, ["prompt"])
    risk = READ  # each of the sub-agent's own tool calls goes through its own permission check

    def parallel_for(self, ctx: ToolContext, args: dict[str, Any]) -> bool:
        return True  # each sub-agent has its own context; task calls in the same reply run at the same time

    def summary(self, args: dict[str, Any]) -> str:
        who = args.get("id") or args.get("name") or args.get("agent") or ""
        bg = " · arka planda" if args.get("background") else ""
        return (f"{who}: " if who else "") + str(args.get("prompt", ""))[:110].replace("\n", " ") + bg

    def run(self, ctx: ToolContext, prompt: str, max_steps: int | None = None, agent: str = "", name: str = "",
            background: bool = False, id: str = "") -> ToolResult:  # noqa: A002 - the name the model sees
        from alice import subagents

        parent = ctx.state.get("agent")
        if parent is None:
            raise ToolError("alt ajan bu bağlamda kullanılamıyor")
        if id:
            sub = subagents.resume(ctx, id, prompt, background=bool(background))
        else:
            sub = subagents.start(parent, ctx, prompt, kind=agent, label=name, background=bool(background),
                                  max_steps=max_steps)
        if sub.background:
            return ToolResult(f"alt ajan {sub.id} ({sub.label}) arka planda çalışıyor. Bitince sonucu bu sohbete "
                              "mesaj olarak gelecek; o zamana kadar ne bulacağını tahmin etme. Kullanıcı ilerlemeyi "
                              "yan paneldeki Ajanlar sekmesinde görür.")
        subagents.wait(sub, ctx.cancelled)
        head = f"[alt ajan {sub.id} · {sub.label}, {sub.steps} adım{'' if sub.status == 'done' else ', ' + sub.status}]"
        return ToolResult(f"{head}\n{sub.result}\n\n(Aynı alt ajana ek soru: task id={sub.id})",
                          is_error=sub.status == "failed")


class AgentDefs(Tool):
    name = "agent_defs"
    description = ("Özel alt ajan tanımlarını yönetir (task aracında agent=<ad> ile çağrılanlar; Claude Code'daki "
                   ".claude/agents gibi). 'Bana bir test yazıcı ajan tanımla', 'inceleyici ajanın talimatını değiştir', "
                   "'hangi ajanlarım var' için.\n"
                   "- save: name, description (ne zaman kullanılacağı, tek cümle), prompt (ajanın sistem talimatı: "
                   "rolü, nasıl çalışacağı, ne döndüreceği), tools (boş: hepsi; salt okuma ajanı için ör. read_file, "
                   "grep, glob, list_dir, git), model (brain, fast ya da sağlayıcı:model), max_steps, where (user: her "
                   "projede, project: yalnız bu projede).\n- list | delete (name).")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "save", "delete"], "description": "Yapılacak iş."},
        "name": {"type": "string", "description": "Ajan adı: küçük harf, rakam, - ve _, ör. 'test-yazici'."},
        "description": {"type": "string", "description": "save: ne yapar, ne zaman çağrılmalı (tek cümle)."},
        "prompt": {"type": "string", "description": "save: ajanın sistem talimatı."},
        "tools": {"type": "array", "items": {"type": "string"}, "description": "save: izinli araçlar; boş: hepsi."},
        "model": {"type": "string", "description": "save: brain, fast ya da sağlayıcı:model; boş: ana ajanınki."},
        "max_steps": {"type": "integer", "description": "save: en fazla adım (varsayılan 40)."},
        "where": {"type": "string", "enum": ["user", "project"], "description": "save: user (varsayılan) ya da "
                  "project."},
    }, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        return WRITE if args.get("action") in ("save", "delete") else READ

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('name') or ''}".strip()

    def run(self, ctx: ToolContext, action: str, name: str = "", description: str = "", prompt: str = "",
            tools: list[str] | None = None, model: str = "", max_steps: int = 40, where: str = "user") -> ToolResult:
        from alice import skills

        try:
            if action == "save":
                a = skills.save_agent(name, description, prompt, tools, model, max_steps, where, ctx.cwd)
                return ToolResult(f"kaydedildi: {a.path}. Artık task agent={a.name} ile çağrılabilir.")
            if action == "delete":
                return ToolResult(f"silindi: {skills.delete_agent(name, ctx.cwd)}")
        except ValueError as e:
            raise ToolError(str(e)) from e
        defs = skills.load_agents(ctx.cwd)
        return ToolResult("\n".join(
            f"{a.name}: {a.description} [araçlar: {', '.join(a.tools) or 'hepsi'}; model: {a.model or 'ana ajan'}; "
            f"{a.max_steps} adım] ({a.path})" for a in defs.values()) or "tanımlı özel alt ajan yok")


class SubagentsTool(Tool):
    name = "subagents"
    description = ("Bu sohbetin alt ajanlarını (task ile başlatılanlar) listeler ya da durdurur. 'Arkadaki ajanlar ne "
                   "durumda', 'şu ajanı durdur' için. Sonuç bekliyorsan sorma: bitince mesajı kendiliğinden gelir.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "stop"], "description": "list: durumlar | stop: id'yi durdur."},
        "id": {"type": "string", "description": "stop: alt ajan (aN)."},
    }, ["action"])
    risk = READ

    def run(self, ctx: ToolContext, action: str, id: str = "") -> ToolResult:  # noqa: A002
        from alice import subagents

        subs = subagents.registry(ctx)
        if action == "stop":
            sub = subs.get(id.strip())
            if sub is None:
                raise ToolError(f"böyle bir alt ajan yok: {id}")
            return ToolResult(f"{sub.id} durduruluyor" if subagents.stop(sub) else f"{sub.id} zaten çalışmıyor")
        rows = [f"{s.id} [{s.status}] {s.label} · {s.steps} adım"
                + (f" · son: {s.last}" if s.status == "running" and s.last else "")
                + (f"\n   sonuç: {s.result[:200]}" if s.result else "") for s in subs.values()]
        return ToolResult("\n".join(rows) or "bu sohbette alt ajan yok")


class Reflex(Tool):
    name = "reflex"
    description = (
        "Ekranda/tarayıcıda bir hedefi Jev'le hızlıca yapar (adım ~1 sn): ekranı okur, her adımda eylemi seçer: "
        "tıklama, sağ/çift tık, alana yazma, pencereye klavyeyle yazma, tuş ya da kombinasyon (ctrl+shift+s), tuşu "
        "basılı tutma, sürükle-bırak, kaydırma. Sonuçta ekranın başa göre değişen yazıları da gelir.\n"
        "- Kullan: 'Impossible şarkısına tıkla', 'Ayarlar > Gizlilik'i aç', 'kartı sağdaki sütuna sürükle'.\n"
        "- Klavye işi tek adımdır: hesap makinesinde tuşlara tıklatma, text='1234*5678=', press_first=['escape'] "
        "ver; kombinasyonları keys'e yaz.\n"
        "- Uygulama açmaz: önce app/windows aracıyla aç, sonra title ver (boşsa öndeki pencere).\n"
        "- En iyi tek işlik hedefte çalışır: uzun işi kısa hedeflere böl, her biri için ayrı çağır; kuralları, "
        "stratejiyi notes'a yaz.\n"
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
        "press_first": {"type": "array", "items": {"type": "string", "description": "Tuş, ör. 'escape'."},
                        "description": "text yazılmadan önce basılacak tuşlar: önceki girdiyi temizlemek için "
                                       "(hesap makinesinde 'escape', alanda 'ctrl+a')."},
        "hold_seconds": {"type": "number", "description": "Tuş basılı tutulacaksa kaç saniye. Varsayılan 1."},
        "allow_irreversible": {"type": "boolean", "description": "Sadece kullanıcı açıkça istediyse true: gönder, "
                               "öde, sil, satın al, onayla/kabul gibi geri alınamaz düğmelere basılabilir. Varsayılan "
                               "false: refleks böyle bir düğmede durur (needs_confirmation)."},
        "max_steps": {"type": "integer", "description": "En fazla adım. Varsayılan 20."},
    }, ["goal", "target"])
    risk = COMPUTER

    def run(self, ctx: ToolContext, goal: str, target: str, url: str | None = None, title: str | None = None,
            inputs: dict[str, str] | None = None, notes: str = "", max_steps: int = 20, text: str = "",
            keys: list[str] | None = None, hold_seconds: float | None = None,
            press_first: list[str] | None = None, allow_irreversible: bool = False) -> ToolResult:
        from alice.config import alice_home
        from alice.reflex.agent import (ScreenAgent, make_consultant, make_planner, make_translator,
                                        multi_part_goal)
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
        loop.allow_irreversible = bool(allow_irreversible)
        loop.consult = make_consultant(ctx.router)  # when stuck, ask from inside; no full main-model turn
        if ((getattr(ctx.config, "data", None) or {}).get("reflex", {}) or {}).get("plan", True):
            loop.plan = make_planner(ctx.router)  # multi-part goals: split into steps, verify each
        extra = {k: v for k, v in (("text", text), ("keys", keys), ("press_first", press_first)) if v}
        res = loop.run(goal, max_steps=max_steps, inputs=inputs, notes=notes or "", **extra)
        status = getattr(res, "status", "")
        verdict = reflex_verdict(status)
        if status in ("max_steps", "needs_help") and multi_part_goal(goal) and "planın" not in getattr(res, "reason", ""):
            # A long goal was tried as one piece and did not finish: the main model must not resend the same long goal.
            verdict += ("\nBu hedef birkaç iş içeriyordu. Aynısını tekrar yollama: yukarıdaki adımlara bakıp nerede "
                        "kaldığını bul ve kalanını tek işlik kısa hedeflerle (her çağrıda bir menü, bir alan, bir tuş) "
                        "sırayla çağır.")
        return ToolResult(res.report() + ("\n" + verdict if verdict else ""), is_error=status == "error")


def reflex_verdict(status: str) -> str:
    """State the status plainly: models confused "Jev cevap verdi" (Jev replied) with "hedef yapıldı" (goal done)
    and told the user "çalışıyor, başardı" (it's working, it succeeded), even on a needs_help result."""
    return {"done": "SONUÇ: hedef yapıldı.",
            "clicked": "SONUÇ: hedef sanılan yere tıklandı ama sonucu doğrulanmadı. Bir ekran görüntüsüyle doğru "
                       "şeyin açıldığına bak; doğruysa bitti, değilse kendin düzelt.",
            "likely_done": "SONUÇ: son eylem ekranı değiştirdi ve karar motoru hedefin büyük ihtimalle yapıldığını "
                           "düşünüyor ama emin değil. Bir ekran görüntüsüyle (ya da dosyaya bakarak) doğrula, sonra "
                           "kullanıcıya söyle.",
            "steps_done": "SONUÇ: hedef adımlara bölündü ve adımlar sırayla yapıldı; hedefin bütününün gerçekleştiği "
                          "ayrıca doğrulanmadı. Sonuca kendin bak (dosya oluştu mu, pencere ne gösteriyor), sonra "
                          "kullanıcıya söyle; doğrulamadan 'yaptım' deme.",
            "needs_help": "SONUÇ: hedef YAPILAMADI (karar motoru cevap verdi ama yol bulamadı). Kullanıcıya başardı "
                          "deme; kalanını kendin yap ya da nedenini söyle.",
            "acted": "SONUÇ: bir eylem yapıldı ve ekran değişti; istenenin olup olmadığı ayrıca doğrulanmalı.",
            "needs_confirmation": "SONUÇ: DURDU, geri alınamaz olabilecek bir düğme (gönder/öde/sil/onayla). Kullanıcı "
                                  "bunu açıkça istemediyse ona sor; istediyse allow_irreversible=true ile tekrar çağır.",
            "needs_login": "SONUÇ: DURDU, giriş/oturum ekranı çıktı (hedef YAPILAMADI). Kullanıcı adına giriş yapma, "
                           "numara/şifre yazma. Uygulamanın masaüstü sürümü kuruluysa onu dene (app open/flow kuruluysa "
                           "kendisi seçer); yoksa kullanıcıya giriş yapması gerektiğini söyle.",
            "max_steps": "SONUÇ: adım sınırı doldu, hedef YAPILAMADI.",
            "error": "SONUÇ: hata, hedef YAPILAMADI."}.get(status, "")


def _last_user_text(ctx: ToolContext) -> tuple[str, ...]:
    """The user's last message: it is visible in the chat window; reflex must not mistake it for a target on screen."""
    for m in reversed(getattr((getattr(ctx, "state", None) or {}).get("agent"), "messages", None) or []):
        if m.get("role") != "user" or m.get("_nudge"):
            continue
        # Memory recalls are added to the content (project names included). When those were also counted as an
        # "echo" (yankı), projects dropped off the sidebar list. The user's own text is in _display.
        text = m.get("_display") or m.get("content")
        if isinstance(text, list):
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
        return (text,) if isinstance(text, str) and text else ()
    return ()


class AskUser(Tool):
    name = "ask_user"
    description = ("İşin ORTASINDA, cevap gelmeden devam edemediğinde kullanıcıya tıklanabilir seçeneklerle soru sorar "
                   "ve cevabı BEKLER (uygulamada kart, terminalde numaralı liste). Örnek: iki makul yol var ve seçim "
                   "sonucu değiştiriyor, ya da gereken bir bilgi (hangi hesap, hangi klasör) eksik. İş BİTTİYSE ve "
                   "sadece sonraki adımı önereceksen bu değil next_step. Önerdiğin seçeneği ilk sıraya koy; kullanıcı "
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
        # Must be defined on the class: sub-agent/voice __getattr__ silently returns a function for any name.
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
    description = ("Kullanıcının ALICE'e öğrettiği bir skill'in (adım adım talimat + yardımcı dosyalar) içeriğini yükler ve "
                   "döndürür.\n"
                   "- Sistem istemindeki 'Skills' listesinde açıklaması işe uyan bir skill varsa, işe başlamadan ÖNCE bunu çağır "
                   "ve dönen talimatlara göre çalış.\n"
                   "- Sadece listede yazan adları kullan; ad uydurma. Aynı turda zaten yüklediysen tekrar çağırma.")
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


# Files shown with send_file: the "Aç / Klasörde göster" (Open / Show in folder) buttons in the UI can only open these.
SENT_FILES: set[str] = set()
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


class SendFile(Tool):
    """Equivalent of SendUserFile in Claude Code."""

    name = "send_file"
    parallel_safe = True
    description = ("Ürettiğin ya da bulduğun dosyaları kullanıcıya sohbette kart olarak gösterir (ad, boyut, Aç ve "
                   "Klasörde göster düğmeleri); görseller sohbette önizlenir.\n"
                   "- Kullanıcının görmek isteyeceği bir sonuç ürettiğinde kullan: rapor, grafik, dışa aktarılan "
                   "belge, ekran görüntüsü, derlenmiş program.\n"
                   "- Ara dosyaları, geçici çıktıları ya da hâlâ üzerinde çalıştığın dosyanın her sürümünü gönderme; "
                   "anlamlı ölçüde değişmedikçe aynı dosyayı tekrar gönderme.\n"
                   "- Dosya önceden diskte olmalı; bu araç dosya oluşturmaz (önce write_file vb.).")
    parameters = obj({
        "paths": {"type": "array", "items": {"type": "string"}, "description": "Gösterilecek dosyaların yolları; "
                  "mutlak ya da çalışma klasörüne göre. En fazla 10."},
        "caption": {"type": "string", "description": "Kartın üstünde tek satırlık not, ör. 'Hata 42. satırda'. "
                    "Dosya kendini anlatıyorsa boş bırak."},
    }, ["paths"])
    risk = READ

    def summary(self, args: dict[str, Any]) -> str:
        return ", ".join(str(p) for p in (args.get("paths") or [])[:3])

    def run(self, ctx: ToolContext, paths: list[str], caption: str = "") -> ToolResult:
        files, images, missing = [], [], []
        for raw in (paths or [])[:10]:
            p = ctx.resolve(str(raw))
            if not p.is_file():
                missing.append(str(p))
                continue
            SENT_FILES.add(str(p))
            files.append({"path": str(p), "name": p.name, "size": p.stat().st_size})
            if p.suffix.lower() in IMAGE_EXT and len(images) < 4:
                try:
                    from alice.tools.imaging import file_to_png_b64

                    images.append(file_to_png_b64(p, int(ctx.config.agent.get("screenshot_max_width", 1366))))
                except Exception:  # noqa: BLE001 - the card is shown even without a preview
                    pass
        if not files:
            raise ToolError("gösterilecek dosya yok: " + ", ".join(missing))
        text = (f"{caption}\n" if caption else "") + "Kullanıcıya gösterildi: " + ", ".join(f["name"] for f in files)
        if missing:
            text += "\nBulunamadı: " + ", ".join(missing)
        return ToolResult(text, images=images, files=files)


class NextStep(Tool):
    """Claude Code's end-of-turn box: short summary, optional question with buttons. Does not block the turn."""

    name = "next_step"
    parallel_safe = True
    description = ("İş BİTTİĞİNDE, son cevabından hemen önce çağrılır: sohbetin altına kısa bir özet kutusu koyar; "
                   "istersen sonraki adım için tek bir soru ve 1-3 düğme ekler. BEKLEMEZ, tur biter; kullanıcı "
                   "düğmeye basarsa o metin yeni mesajı olarak gelir. Devam etmek için cevaba ihtiyacın varsa bu "
                   "değil ask_user.\n"
                   "- Birkaç adımlı işlerin sonunda kullan; kısa sorular ve sohbet için gerek yok.\n"
                   "- recap: şu an ne durumda, ne yapılmadı/doğrulanmadı (2-3 kısa cümle).\n"
                   "- question: kullanıcıya kalan tek karar; önerdiğin seçenek ilk düğme.")
    parameters = obj({
        "recap": {"type": "string", "description": "2-3 kısa cümle: ne değişti, ne durumda, ne eksik/doğrulanmadı."},
        "question": {"type": "string", "description": "Kullanıcıya kalan tek karar (en fazla 100 karakter). Yoksa boş."},
        "options": {"type": "array", "items": {"type": "string", "description": "Düğme metni (1-4 kelime)."},
                    "description": "question'ın cevapları; önerilen ilk sırada. En fazla 3."},
    }, ["recap"])
    risk = READ

    def run(self, ctx: ToolContext, recap: str, question: str = "", options: list[str] | None = None) -> ToolResult:
        recap = " ".join(str(recap).split())[:400]
        question = " ".join(str(question or "").split())[:140]
        opts = [" ".join(str(o).split())[:40] for o in (options or []) if str(o).strip()][:3] if question else []
        return ToolResult("Özet kutusu gösterildi. Son cevabında özeti tekrarlama; soruyu tekrar sorma.",
                          card={"kind": "next_step", "recap": recap, "question": question, "options": opts})


class ExitPlanMode(Tool):
    """Equivalent of ExitPlanMode in Claude Code: presents the plan prepared in plan mode for approval."""

    name = "exit_plan_mode"
    description = ("Plan modunda hazırladığın uygulama planını kullanıcıya onay için sunar. Onaylanırsa plan modu "
                   "kapanır ve dosya yazma, komut çalıştırma serbest kalır; reddedilirse kullanıcının notuyla planı "
                   "düzelt.\n"
                   "- Sadece plan modundayken ve kod/dosya değişikliği gerektiren bir işin planı hazır olduğunda "
                   "kullan. Araştırma, soru cevaplama ya da sadece okuma işleri için kullanma.\n"
                   "- Planda neyin, hangi dosyalarda, hangi sırayla değişeceğini ve nasıl doğrulanacağını yaz. "
                   "\"Plan uygun mu?\" diye ayrıca ask_user ile sorma; bu araç zaten onay ister.\n"
                   "- Belirsiz bir tercih varsa önce ask_user ile netleştir, sonra planı sun.")
    parameters = obj({
        "plan": {"type": "string", "description": "Uygulama planı (Markdown): adımlar, değişecek dosyalar, doğrulama."},
    }, ["plan"])
    risk = READ

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("plan", "")).strip().splitlines()[0][:80] if args.get("plan") else ""

    def run(self, ctx: ToolContext, plan: str) -> ToolResult:
        agent = ctx.state.get("agent")
        perms = getattr(agent, "permissions", None)
        if perms is None or getattr(perms, "mode", "") != "plan":
            raise ToolError("plan modunda değilsin; işe doğrudan devam et")
        ui = getattr(agent, "ui", None)
        ask = getattr(ui, "ask_choice", None) if callable(getattr(type(ui), "ask_choice", None)) else None
        if not callable(ask):
            return ToolResult("Bu arayüzde onay kartı gösterilemiyor. Planı cevabında yaz ve kullanıcının onayını "
                              "bekle; onay gelmeden dosya değiştirme.")
        approve, change = "Onayla, uygula", "Değiştir"
        answer = (ask("Plan onaylansın mı?\n\n" + plan.strip()[:6000], [approve, change], False) or "").strip()
        if answer == approve:
            back = getattr(perms, "before_plan", None) or str(ctx.config.agent.get("permission_mode", "auto"))
            perms.mode = back if back != "plan" else "auto"
            return ToolResult(f"Kullanıcı planı onayladı; plan modu kapandı ({perms.mode}). Plana göre uygula.")
        if not answer:
            return ToolResult("Kullanıcı cevap vermedi. Plan modunda kal; dosya değiştirme.")
        return ToolResult(f"Kullanıcı planı onaylamadı: {answer}. Plan modunda kal, notuna göre planı düzelt ve "
                          "tekrar sun.")


TOOLS = [TodoWrite(), Memory(), Task(), SubagentsTool(), AgentDefs(), Reflex(), Projects(), SkillTool(), AskUser(),
         Feedback(), SendFile(), NextStep(), ExitPlanMode()]
