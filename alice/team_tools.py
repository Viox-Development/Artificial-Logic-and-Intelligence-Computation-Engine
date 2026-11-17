"""Project tools: the Tool classes used by the agents (team_send, team_wait, design_check, site_study) and by the lead
(plan, assign, review, diff, ask_user, finish, project_status)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alice.team import (
    ACTIVE,
    COUNCIL,
    DESIGN_FILE,
    ISOLATIONS,
    LEAD,
    MAX_MEMBERS,
    NAME_RX,
    RESEARCH_DIR,
    SYSTEM,
    THINKING,
    USER,
    research_refs,
)
from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult, obj

if TYPE_CHECKING:
    from alice.team import Team


class TeamSend(Tool):
    name = "team_send"
    description = ("Takım arkadaşına, yöneticiye (alice) ya da herkese mesaj gönderir. Arayüz/sözleşme duyurmak, karar "
                   "sormak, başkasının alanında bir değişiklik istemek için. Kısa ve net yaz.")
    parameters = obj({
        "to": {"type": "string", "description": f"Ajan adı, '{LEAD}' (yönetici) ya da 'all' (herkes)."},
        "text": {"type": "string", "description": "Mesaj."},
    }, ["to", "text"])
    risk = READ

    def __init__(self, team: Team, me: str):
        self.team, self.me = team, me
        if me == LEAD:
            self.name = "message"
            self.description = ("Ajana ya da herkese ('all') mesaj gönderir: soruya cevap, yön değişikliği, yeni bilgi. "
                                "Bitmiş ajana yazınca kaldığı yerden devam eder.")

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('to')}: {str(args.get('text', ''))[:120]}"

    def run(self, ctx: ToolContext, to: str, text: str) -> ToolResult:
        return ToolResult(self.team.send(self.me, str(to).strip().lower(), text))


class DesignCheck(Tool):
    name = "design_check"
    description = ("Sitenin masaüstü ve telefon ekran görüntülerini alır, yapay zekâ yapımı görünen kalıpları koddan sayar "
                   "ve görüntüleri bir sanat yönetmenine (görebilen model) eleştirtir. Görünüşe dokunan işi bitirmeden "
                   "önce çalıştır, çıkan sorunları düzelt.")
    parameters = obj({"focus": {"type": "string", "description": "İsteğe bağlı: özellikle neye bakılsın (sayfa, bölüm)."}})
    risk = READ
    MAX_CRITIQUES = 4  # per agent; after that, only pattern counting and the image

    def __init__(self, team: Team, me: str):
        self.team, self.me = team, me
        self.critiques = 0

    def run(self, ctx: ToolContext, focus: str = "") -> ToolResult:
        from alice.design_check import critique, lint, report
        from alice.site import find_entry, screenshots

        root = ctx.cwd
        entry = find_entry(root) or find_entry(self.team.cwd)
        if not entry:
            return ToolResult("Sitenin giriş sayfası (index.html) bulunamadı; önce sayfayı oluştur.", is_error=True)
        base = root if find_entry(root) else self.team.cwd
        slop = report(lint(base))
        shots = screenshots(base, entry)
        parts = [f"Giriş sayfası: {entry}"]
        parts.append("Kalıp denetimi: temiz." if not slop else "Kalıp denetimi — düzeltilmesi gerekenler:\n" + slop)
        if not shots:
            parts.append("Ekran görüntüsü alınamadı (bilgisayarda Edge/Chrome ya da Playwright bulunamadı); sadece "
                         "kalıp denetimi yapıldı.")
        elif self.critiques < self.MAX_CRITIQUES:
            self.critiques += 1
            design = self.team.design_brief(self.team.members[self.me]) if self.me in self.team.members else ""
            goal = self.team.goal + (f"\nÖzellikle bak: {focus}" if focus else "")
            text = critique(ctx.router, shots, goal, design, slop, refs=research_refs(self.team.cwd))
            if text:
                parts.append("Sanat yönetmeninin eleştirisi:\n" + text)
                self.team.mark(self.me, "design", f"design_check: {text.splitlines()[0][:140]}")
        return ToolResult("\n\n".join(parts), images=list(shots.values()))


class SiteStudy(Tool):
    name = "site_study"
    description = ("Gerçek bir web sitesine bakar: masaüstü, uzun (sayfanın aşağısı) ve telefon ekran görüntülerini alır ve "
                   "görebilen bir modele çözümletir: düzen, tipografi ölçüleri, renk oranları, görsel dil, metin tonu ve "
                   "bizim siteye alınacaklar. Tasarım yönünü belirlemeden önce konunun iyi örneklerini bununla incele. "
                   "Görüntüler ve çözümleme research/ klasörüne kaydedilir.")
    parameters = obj({"url": {"type": "string", "description": "İncelenecek sitenin adresi (https://...)."},
                      "focus": {"type": "string", "description": "İsteğe bağlı: özellikle neye bakılsın."}}, ["url"])
    risk = READ
    MAX_STUDIES = 8  # per agent

    def __init__(self, team: Team, me: str):
        self.team, self.me = team, me
        self.studies = 0

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("url", ""))[:120]

    def run(self, ctx: ToolContext, url: str, focus: str = "") -> ToolResult:
        from urllib.parse import urlparse

        from alice.design_check import study
        from alice.site import STUDY_VIEWPORTS, capture, reachable

        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        host = urlparse(url).hostname or ""
        if not host or host in ("localhost", "127.0.0.1") or host.startswith(("10.", "192.168.", "169.254.")):
            return ToolResult("Sadece internetteki gerçek siteler incelenebilir.", is_error=True)
        if self.studies >= self.MAX_STUDIES:
            return ToolResult(f"Bu ajan için {self.MAX_STUDIES} site incelendi; elindekilerle devam et.", is_error=True)
        ok, info = reachable(url)
        if not ok:
            return ToolResult(f"{url} açılamadı ({info}); başka bir referans dene.", is_error=True)
        shots = capture(info, timeout=40, settle=2500, viewports=STUDY_VIEWPORTS)
        if not shots:
            return ToolResult("Ekran görüntüsü alınamadı (Edge/Chrome ya da Playwright yok). web_fetch ile metnine "
                              "bakabilirsin ama görünüşü hakkında tahmin yürütme.", is_error=True)
        self.studies += 1
        slug = re.sub(r"[^a-z0-9]+", "-", host.lower().removeprefix("www.")).strip("-")[:40] or "site"
        out_dir = self.team.cwd / RESEARCH_DIR
        import base64

        try:
            out_dir.mkdir(exist_ok=True)
            for name, b64 in shots.items():
                (out_dir / f"{slug}-{name.replace('ü', 'u').replace('ş', 's')}.png").write_bytes(base64.b64decode(b64))
        except OSError:
            pass
        text = study(ctx.router, shots, info, self.team.goal, focus)
        if text:
            try:
                (out_dir / f"{slug}.md").write_text(f"# {info}\n\n{text}\n", encoding="utf-8")
            except OSError:
                pass
            self.team.mark(self.me, "design", f"site_study {host}: {text.splitlines()[0][:120]}")
        else:
            text = ("(Görsel model çözümleme yapamadı; görüntüler ekte. Görebiliyorsan kendin çözümle, göremiyorsan "
                    "bu siteyi referans olarak kullanma.)")
        return ToolResult(f"{info} incelendi; görüntüler {RESEARCH_DIR}/{slug}-*.png\n\n{text}",
                          images=[shots[k] for k in ("masaüstü", "telefon") if k in shots])


class TeamWait(Tool):
    name = "team_wait"
    description = ("Sana mesaj gelene kadar bekler (en çok `seconds` saniye) ve gelenleri döndürür. Bir soruya cevap "
                   "beklerken ya da başka bir ajanın işine bağlıyken kullan; boşuna bekleme.")
    parameters = obj({"seconds": {"type": "integer", "description": "En çok kaç saniye. Varsayılan 120, en çok 600."}})
    risk = READ

    def __init__(self, team: Team, me: str):
        self.team, self.me = team, me

    def run(self, ctx: ToolContext, seconds: int = 120) -> ToolResult:
        m = self.team.members[self.me]
        m.status = "asking" if m.question else "waiting"
        self.team._changed()
        try:
            msgs = self.team.wait_messages(self.me, max(1, min(int(seconds or 120), 600)), ctx.cancelled)
        finally:
            if m.status in ("asking", "waiting"):
                m.status = "running"
            self.team._changed()
        if not msgs:
            return ToolResult("süre doldu, mesaj gelmedi. Bekleyebileceğin başka bir şey yoksa işine devam et.")
        return ToolResult("\n".join(x.line() for x in msgs))


def _tree(root: Path, limit: int = 80) -> str:
    """Short folder summary the council and the lead will see (two levels; hidden and large folders excluded)."""
    from alice.site import SKIP_DIRS

    rows: list[str] = []
    try:
        for p in sorted(root.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if p.name.startswith(".") or p.name in SKIP_DIRS:
                continue
            if p.is_dir():
                try:
                    kids = []
                    for k in p.iterdir():
                        if not k.name.startswith("."):
                            kids.append(k.name)
                        if len(kids) >= 40:
                            break
                    kids = sorted(kids)[:12]
                except OSError:
                    kids = ["(okunamadı)"]
                rows.append(f"{p.name}/  ({', '.join(kids) or 'boş'})")
            else:
                rows.append(p.name)
            if len(rows) >= limit:
                rows.append("…")
                break
    except OSError:
        pass
    return "\n".join(rows) or "(klasör boş)"


def route_plan_notes(snap: dict[str, Any], specs: list[dict[str, Any]]) -> list[str]:
    """Adds the plan council's notes to the agent's task as 'kurul notu'; returns the notes no agent owns."""
    from alice.council import dedupe

    by_name = {str(a.get("name") or "").strip().lower(): a for a in specs}
    general: list[str] = []
    for f in snap.get("findings", []):
        if f.get("status") == "refuted":
            continue
        text = f"{f['title']}" + (f" → {f['fix']}" if f.get("fix") else "")
        spec = by_name.get(str(f.get("agent") or "").strip().lower())
        if spec is None:
            general.append(text)
            continue
        old = str(spec.get("constraints") or "").strip()
        if dedupe([text], [x for x in old.splitlines() if x.strip()]):
            spec["constraints"] = (old + "\n" if old else "") + f"Kurul notu: {text}"
    general += list((snap.get("result") or {}).get("conditions") or [])
    return dedupe(general)


class LeadPlan(Tool):
    name = "plan"
    description = ("Planı ve ajanları birden sunar. Kurul açıksa plan önce inceleme kurulunda tartışılıp oylanır: "
                   "onaylanırsa ajanlar hemen başlar; reddedilirse eleştiriler döner, planı düzeltip tekrar sun. "
                   "Kurul kapalıysa ajanlar doğrudan başlar.")
    parameters = obj({
        "summary": {"type": "string", "description": "Yaklaşım: işi nasıl böldün, neden, riskler, doğrulama yolu."},
        "agents": {"type": "array", "description": "Ajanlar (en çok 8); assign ile aynı alanlar.",
                   "items": {"type": "object"}},
        "skip_council": {"type": "boolean", "description": "Sadece kullanıcı planı açıkça onayladıysa true."},
    }, ["summary", "agents"])
    risk = READ

    def __init__(self, team: Team):
        self.team = team

    def summary(self, args: dict[str, Any]) -> str:
        names = ", ".join(str(a.get("name")) for a in args.get("agents") or [] if isinstance(a, dict))
        return f"plan: {names}"

    def run(self, ctx: ToolContext, summary: str, agents: list[dict[str, Any]], skip_council: bool = False) -> ToolResult:
        from alice.council import report

        t = self.team
        specs = [dict(a) for a in agents or [] if isinstance(a, dict)]  # copy: keep council notes out of the lead's history
        if not specs:
            raise ToolError("en az bir ajan gerekli")
        if len(specs) + len(t.members) > MAX_MEMBERS:
            raise ToolError(f"bir projede en çok {MAX_MEMBERS} ajan olur")
        names = [str(a.get("name") or "").strip().lower() for a in specs]
        bad = [n for n in names if not NAME_RX.match(n) or n in (LEAD, "all", USER, SYSTEM, COUNCIL) or n in t.members]
        if bad or len(set(names)) != len(names):
            raise ToolError(f"geçersiz ya da tekrarlanan ajan adı: {', '.join(bad) or 'tekrar var'}")
        _check_specs(t, specs, names)  # don't convene the council for nothing: definition errors first
        cond_note = ""
        if t.council and t.plan_council and not skip_council:
            lines = [f"- {a.get('name')}: {a.get('task')} | model={a.get('model') or 'auto'} "
                     f"düşünme={a.get('thinking', 'default')} yer={a.get('isolation', 'shared')} "
                     f"kapsam={a.get('scope') or 'her yer'} sonra={a.get('after') or '-'} sınır={a.get('constraints') or '-'}"
                     for a in specs]
            context = (f"# Yaklaşım\n{summary}\n\n# Ajanlar\n" + "\n".join(lines) +
                       f"\n\n# Proje klasörü ({t.cwd})\n{_tree(t.cwd)}")
            if t.is_design():
                context += (f"\n\n# Not\nBu bir tasarım işi: plan kod yazılmadan önce özgün bir tasarım yönü "
                            f"({DESIGN_FILE}) belirleyen bir adımla başlamalı ve sayfa ajanları ona bağlı (after) olmalı. "
                            f"Bu adım yoksa 'tasarım' alanında önemli bulgu yaz.")
            # A revised plan is reviewed by the council that rejected it (found by its id).
            prev = next((c for c in reversed(t.councils) if c.get("id") == t.plan_prev_id), None) \
                if t.plan_rejects and t.plan_prev_id else None
            with t.cond:  # council threads decrement under the lock; lost unlocked increments would stick the counter
                t.council_busy += 1
            try:
                snap = t._council("plan", f"Proje planı: {t.goal}", context, t.councils, previous=prev)
            finally:
                with t.cond:
                    t.council_busy -= 1
            if snap.get("state") == "stopped":
                return ToolResult("Proje durduruldu; plan kurulu yarıda kaldı. Sürdürülünce planı tekrar sun.",
                                  is_error=True)
            failed = snap.get("state") == "failed"  # if the council crashed, the plan does not count as rejected
            dec = (snap.get("result") or {}).get("decision")
            if not failed and dec not in ("approve", "approve_conditions") and t.plan_rejects < 1:
                t.plan_rejects += 1
                t.plan_prev_id = str(snap.get("id") or "")
                return ToolResult("Kurul planı onaylamadı. Engelleyici bulguları gider (sadece onları; ayrıntı avına "
                                  "çıkma), planı düzeltip plan ile tekrar sun.\n\n" + report(snap), is_error=True)
            general = route_plan_notes(snap, specs)
            if failed:
                general.insert(0, "Plan kurulu çalışamadı; plan kurulsuz başlatıldı.")
            elif dec not in ("approve", "approve_conditions"):
                note = "Plan kurulu düzeltilmiş plana da itiraz etti; plan kurulun notlarıyla başlatıldı."
                t.mark(COUNCIL, "plan", note)
                t._emit(f"{t.title}: {note}", kind="info")
                general.insert(0, note)
            cond_note = "\n\n" + report(snap) + (("\n\nGenel notlar (ajanlara dağıtılmadı, sen göz önünde tut):\n" +
                                                   "\n".join(f"- {x}" for x in general)) if general else "")
        t.plan_rejects, t.plan_prev_id = 0, ""  # the plan is starting: the next plan is a new plan
        added = []
        for spec in specs:
            added.append(t.add_member(spec, announce=False).name)
        return ToolResult(f"Plan onaylandı, ajanlar başladı: {', '.join(added)}{cond_note}")


def _check_specs(t: Team, specs: list[dict[str, Any]], names: list[str]) -> None:
    """Validate agent definitions before the council convenes (task, workplace, thinking, model, order)."""
    known = set(names) | set(t.members)
    for a, name in zip(specs, names):
        if not str(a.get("task") or "").strip():
            raise ToolError(f"{name}: task boş")
        if str(a.get("isolation") or "shared") not in ISOLATIONS:
            raise ToolError(f"{name}: isolation {', '.join(ISOLATIONS)} olmalı")
        if str(a.get("thinking") or "default") not in THINKING:
            raise ToolError(f"{name}: thinking {', '.join(THINKING)} olmalı")
        t._check_model(str(a.get("model") or "auto").strip(), name)
        unknown = [str(x).strip().lower() for x in a.get("after") or [] if str(x).strip().lower() not in known]
        if unknown:
            raise ToolError(f"{name}: after içinde olmayan ajan: {', '.join(unknown)}")
        if names.index(name) < len(names) and any(names.index(str(x).strip().lower()) > names.index(name)
                                                  for x in a.get("after") or [] if str(x).strip().lower() in names):
            raise ToolError(f"{name}: after'daki ajanlar listede ondan önce gelmeli")


class LeadAssign(Tool):
    name = "assign"
    description = ("Projeye yeni ajan ekler ve hemen başlatır (after verilmişse onlar onaylanınca başlar). Görevi, "
                   "konuşmayı görmeyen birinin anlayacağı netlikte yaz.")
    parameters = obj({
        "name": {"type": "string", "description": "Kısa ad, ör. 'backend', 'tests', 'ui'."},
        "task": {"type": "string", "description": "Yapacağı iş: hangi dosyalar, ne değişecek, nasıl doğrulayacak, ne "
                 "raporlayacak."},
        "constraints": {"type": "string", "description": "Sınırlar: dokunmayacağı yerler, uyacağı kurallar."},
        "scope": {"type": "array", "items": {"type": "string"},
                  "description": "Yazabileceği dosya/klasörler (çalışma klasörüne göre). Boş: her yer."},
        "model": {"type": "string", "description": "Boş/auto (önerilen): işe göre en uygun model seçilir. "
                  "Ya da brain (güçlü), fast (hızlı), sağlayıcı:model."},
        "thinking": {"type": "string", "enum": list(THINKING), "description": "Düşünme seviyesi."},
        "isolation": {"type": "string", "enum": list(ISOLATIONS),
                      "description": "shared: aynı klasör | worktree: kendi git dalı (onayda birleştirilir) | "
                      "readonly: sadece inceleme."},
        "after": {"type": "array", "items": {"type": "string"},
                  "description": "Bu ajanlar onaylanınca başlasın (ör. testler backend'den sonra)."},
        "max_steps": {"type": "integer", "description": "En çok araç adımı. Varsayılan 60."},
    }, ["name", "task"])
    risk = READ

    def __init__(self, team: Team):
        self.team = team

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('name')}: {str(args.get('task', ''))[:110]}"

    def run(self, ctx: ToolContext, **spec: Any) -> ToolResult:
        m = self.team.add_member(spec)
        when = f"; {', '.join(m.after)} onaylanınca başlayacak" if m.status == "queued" and m.after else ""
        return ToolResult(f"{m.name} eklendi ({m.status}{when})")


class LeadReview(Tool):
    name = "review"
    description = ("İnceleme bekleyen ajanın işini onaylar ya da düzeltmeye gönderir. Onaylanan worktree dalı ana "
                   "çalışma klasörüne birleştirilir.")
    parameters = obj({
        "name": {"type": "string", "description": "Ajan adı."},
        "decision": {"type": "string", "enum": ["approve", "revise"], "description": "approve: onayla | revise: "
                     "düzeltme iste."},
        "feedback": {"type": "string", "description": "revise için: neyin yanlış/eksik olduğu ve ne beklediğin."},
    }, ["name", "decision"])
    risk = READ

    def __init__(self, team: Team):
        self.team = team

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('name')}: {args.get('decision')}"

    def run(self, ctx: ToolContext, name: str, decision: str, feedback: str = "") -> ToolResult:
        return ToolResult(self.team.review_member(str(name).strip().lower(), decision == "approve", feedback))


class LeadDiff(Tool):
    name = "diff"
    description = "Ajanın dosya araçlarıyla yaptığı değişikliklerin farkını (unified diff) gösterir."
    parameters = obj({"name": {"type": "string", "description": "Ajan adı."}}, ["name"])
    risk = READ
    parallel_safe = True

    def __init__(self, team: Team):
        self.team = team

    def run(self, ctx: ToolContext, name: str) -> ToolResult:
        return ToolResult(self.team.diff(str(name).strip().lower(), limit=20_000) or "fark yok")


class LeadAskUser(Tool):
    name = "ask_user"
    description = ("Kullanıcıya soru sorar (Projeler sayfasında görünür). Sadece kullanıcının tercihine bağlı, senin "
                   "karar veremeyeceğin şeyler için. Cevap gelince sana olay olarak gelir; bu arada turunu bitir.")
    parameters = obj({"question": {"type": "string", "description": "Kısa, net soru; gerekiyorsa seçenekleriyle."}},
                     ["question"])
    risk = READ

    def __init__(self, team: Team):
        self.team = team

    def run(self, ctx: ToolContext, question: str) -> ToolResult:
        if not (question or "").strip():
            raise ToolError("soru boş")
        self.team.ask_user(question)
        return ToolResult("soru kullanıcıya iletildi; cevap gelince sana olay olarak gelecek.")


class LeadFinish(Tool):
    name = "finish"
    description = ("Projeyi bitirir ve kullanıcıya özet yazar. Ancak bütün işler onaylanıp bütün doğrulandıktan sonra "
                   "çağır.")
    parameters = obj({"summary": {"type": "string", "description": "Ne yapıldı, hangi dosyalar, nasıl doğrulandı, "
                                  "açık kalanlar. Markdown olabilir."}}, ["summary"])
    risk = READ

    def __init__(self, team: Team):
        self.team = team

    def run(self, ctx: ToolContext, summary: str) -> ToolResult:
        if self.team.council_busy:
            return ToolResult("kurul hâlâ toplanıyor; sonucu sana olay olarak gelecek, sonra bitir.", is_error=True)
        open_ = [m.name for m in self.team.members.values() if m.status in ACTIVE + ("review", "queued")]
        if open_:
            return ToolResult(f"henüz bitmemiş ya da incelenmemiş ajanlar var: {', '.join(open_)}. Önce onları "
                              "bitir/onayla ya da durdur.", is_error=True)
        self.team.finish(summary or "")
        return ToolResult("proje bitti; özet kullanıcıya iletildi.")


class LeadStatus(Tool):
    name = "project_status"
    description = "Projedeki ajanların güncel durumu."
    parameters = obj({})
    risk = READ
    parallel_safe = True

    def __init__(self, team: Team):
        self.team = team

    def run(self, ctx: ToolContext) -> ToolResult:
        return ToolResult(self.team.status_text())
