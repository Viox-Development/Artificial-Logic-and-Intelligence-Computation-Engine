"""Çekirdekle konuşan araçlar: geçmiş metrikler, kurallar, zamanlanmış görevler, mod.

Böylece "dün DCS oynarken GPU kaç dereceydi?", "nabzım uzun süre yüksek kalırsa beni uyar",
"her pazar gece projelerimi yedekle" gibi istekleri beyin modeli doğrudan karşılar.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from alice.tools.base import DANGER, NETWORK, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


def _store(ctx: ToolContext):
    from alice.core.store import Store

    st = ctx.state.get("core_store")
    if st is None:
        st = ctx.state["core_store"] = Store()
    return st


class Metrics(Tool):
    name = "metrics"
    parallel_safe = False  # sqlite bağlantısı ctx.state'te paylaşılıyor
    description = ("Çekirdeğin kaydettiği GEÇMİŞ ölçümler ve olaylar: bilgisayar (pc.*), uçuş (dcs.*), saat/nabız "
                   "(watch.hr) gibi. 'Dün DCS'te GPU kaç dereceydi', 'son 2 saatte CPU' gibi sorular için. Şu anki "
                   "durum için system_info. Anahtarı bilmiyorsan önce action=keys.\n"
                   "Ör. dün uçuşta GPU sıcaklığı: action=stats key=pc.gpu.temp since=dün until=bugün mode=flight.")
    parameters = obj({
        "action": {"type": "string", "enum": ["status", "keys", "stats", "series", "events", "modes"],
                   "description": "status: çekirdek çalışıyor mu, hangi mod | keys: pattern'e uyan anahtarlar | stats: "
                   "key için en düşük/ortalama/en yüksek | series: key'in zaman içindeki değerleri | events: olay "
                   "günlüğü | modes: modların açık olduğu zaman aralıkları."},
        "key": {"type": "string", "description": "stats/series için anahtar, ör. 'pc.gpu.temp'; stats'ta glob olabilir "
                "('pc.disk.*.percent')."},
        "pattern": {"type": "string", "description": "keys için glob, ör. 'pc.*'. Varsayılan '*'."},
        "since": {"type": "string", "description": "Başlangıç: '15m', '2h', '3d' (şu andan geriye), 'dün', 'bugün' "
                  "ya da ISO tarih."},
        "until": {"type": "string", "description": "Bitiş, since ile aynı biçim. Boş: şimdi."},
        "mode": {"type": "string", "enum": ["normal", "dev", "flight", "quiet"],
                 "description": "Sadece bu mod açıkken alınan ölçümler (flight = DCS/War Thunder)."},
    }, ["action"])

    def run(self, ctx: ToolContext, action: str, key: str | None = None, pattern: str = "*",
            since: str | None = None, until: str | None = None, mode: str | None = None) -> ToolResult:
        from alice.core.service import read_state
        from alice.core.store import parse_time

        try:
            t0, t1 = parse_time(since), parse_time(until)
        except ValueError as e:
            raise ToolError(str(e)) from e
        st = _store(ctx)
        if action == "status":
            s = read_state()
            if not s.get("running"):
                return ToolResult("çekirdek çalışmıyor (başlatmak için: alice core run, kalıcı için: alice core install)")
            p = s.get("policy", {})
            return ToolResult(f"çekirdek çalışıyor, mod: {s.get('mode')}{' (elle sabit)' if s.get('forced') else ''}, "
                              f"en düşük bildirim: {p.get('min_severity')}, ses: {p.get('voice')}, "
                              f"yerel GPU: {p.get('local_gpu')}")
        if action == "keys":
            ks = st.keys(pattern or "*", since=t0)
            return ToolResult("\n".join(ks) or "veri yok")
        if action == "events":
            evs = [e for e in st.events(since=t0, limit=100) if t1 is None or e.ts <= t1]
            return ToolResult("\n".join(e.line() for e in evs) or "olay yok")
        if action == "modes":
            out = []
            for m in [mode] if mode else ["flight", "dev", "quiet"]:
                for a, b in st.mode_spans(m, t0, t1):
                    out.append(f"{m}: {datetime.fromtimestamp(a):%d.%m %H:%M} - {datetime.fromtimestamp(b):%H:%M} "
                               f"({(b - a) / 60:.0f} dk)")
            return ToolResult("\n".join(out) or "bu aralıkta mod kaydı yok")
        if not key:
            raise ToolError("key gerekli (anahtarları görmek için action=keys)")
        if action == "stats":
            keys = st.keys(key, since=t0) if any(c in key for c in "*?[") else [key]
            return ToolResult("\n".join(st.stats(k, t0, t1, mode).line() for k in keys[:30]) or "veri yok")
        pts = st.series(key, t0, t1, mode)
        if not pts:
            return ToolResult("veri yok")
        step = max(1, len(pts) // 60)
        rows = [f"{datetime.fromtimestamp(t):%d.%m %H:%M:%S} {v:.1f}" for t, v in pts[::step]]
        return ToolResult(f"{key}, {len(pts)} ölçüm" + (f" (her {step}. gösteriliyor)" if step > 1 else "") + "\n"
                          + "\n".join(rows))


class Rules(Tool):
    name = "rules"
    description = ("Ölçüme bağlı uyarı kuralları: 'GPU 85 dereceyi geçerse', 'nabzım 10 dakika 120'nin üstünde "
                   "kalırsa beni uyar'. Çekirdek ölçümleri izler, koşul sağlanınca bildirir. Belirli bir SAATTE "
                   "hatırlatma ya da iş için schedule kullan. Metrik anahtarını bilmiyorsan önce metrics action=keys.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "add", "remove", "enable", "disable"],
                   "description": "list | add: yeni kural (metric, op, threshold, message gerekli) | "
                   "remove/enable/disable: id'li kural."},
        "id": {"type": "string", "description": "remove/enable/disable için kural numarası (list çıktısından)."},
        "metric": {"type": "string", "description": "İzlenecek anahtar ya da glob, ör. 'pc.gpu.temp', "
                   "'pc.disk.*.percent', 'watch.hr'."},
        "op": {"type": "string", "enum": [">", ">=", "<", "<=", "==", "!="], "description": "Karşılaştırma."},
        "threshold": {"type": "number", "description": "Eşik değer, ör. 85."},
        "duration": {"type": "number", "description": "Koşulun kesintisiz sürmesi gereken saniye. Varsayılan 0."},
        "cooldown": {"type": "number", "description": "Tekrar uyarmadan önce beklenecek saniye. Varsayılan 1800."},
        "message": {"type": "string", "description": "Bildirim metni; {value}, {key}, {match}, {threshold} yer "
                    "tutucuları kullanılabilir, ör. 'GPU {value} derece!'."},
        "severity": {"type": "string", "enum": ["info", "warn", "critical"], "description": "Önem. Varsayılan warn."},
        "topic": {"type": "string", "description": "Konu etiketi, ör. 'pc', 'flight', 'health'. Varsayılan pc."},
        "modes": {"type": "array", "items": {"type": "string"},
                  "description": "Sadece bu modlarda çalışsın, ör. [\"flight\"]. Boş: her modda."},
        "confirm": {"type": "string", "description": "Yanlış alarm riski varsa uyarmadan önce hızlı modele sorulacak "
                    "evet/hayır sorusu."},
        "rule_action": {"type": "string", "enum": ["notify", "agent"],
                        "description": "notify: sadece bildir (varsayılan) | agent: ALICE prompt'taki işi yapsın."},
        "prompt": {"type": "string", "description": "rule_action=agent için ALICE'in yapacağı işin tarifi."},
        "description": {"type": "string", "description": "Kullanıcının kendi cümlesi, ör. 'GPU ısınırsa söyle'."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        if args.get("action") == "list":
            return READ
        # Tetiklenince gözetimsiz ajan çalıştıran kural, onay ister.
        return DANGER if args.get("rule_action") == "agent" else WRITE

    def run(self, ctx: ToolContext, action: str, id: str | None = None, rule_action: str = "notify",
            **fields: Any) -> ToolResult:
        from alice.core.rules import Rule, RuleSet

        rs = RuleSet()
        if action == "list":
            return ToolResult("\n".join(r.summary() for r in rs.rules) or "kural yok")
        if action == "add":
            for req in ("metric", "op", "threshold", "message"):
                if fields.get(req) in (None, ""):
                    raise ToolError(f"add için {req} gerekli")
            try:
                rule = Rule.from_dict({**{k: v for k, v in fields.items() if v is not None}, "action": rule_action,
                                       **({"id": id} if id else {})})
            except (TypeError, ValueError) as e:
                raise ToolError(str(e)) from e
            rs.add(rule)
            return ToolResult(f"kural kuruldu: {rule.summary()}")
        if not id:
            raise ToolError("id gerekli")
        ok = rs.remove(id) if action == "remove" else rs.set_enabled(id, action == "enable")
        return ToolResult("tamam" if ok else f"kural yok: {id}", is_error=not ok)


class Schedule(Tool):
    name = "schedule"
    description = ("Belirli saatlerde ya da tekrarlı çalışan görevler: 'her sabah 8'de hava durumunu söyle', 'her "
                   "pazar gece projelerimi yedekle', 'yarın 17:00'de bana toplantıyı hatırlat'. O saatte ALICE "
                   "prompt'taki işi gözetimsiz yapar. Kurmadan önce ne zaman ve ne yapılacağını kullanıcıya söyle. "
                   "Ölçüme bağlı uyarılar (sıcaklık, nabız) için rules kullan.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "add", "remove", "run_now"],
                   "description": "list | add: yeni görev (cron ve prompt gerekli) | remove: id'li görevi sil | "
                   "run_now: id'li görevi hemen çalıştır."},
        "id": {"type": "string", "description": "remove/run_now için görev numarası (list çıktısından)."},
        "cron": {"type": "string", "description": "5 alanlı cron: dakika saat gün ay haftanın-günü (0 = pazar). "
                 "Ör. '0 8 * * *' her gün 08:00, '30 22 * * 0' pazar 22:30, '0 17 11 10 *' 11 Ekim 17:00."},
        "prompt": {"type": "string", "description": "O saatte yapılacak işin eksiksiz tarifi; konuşmayı görmeyen "
                   "birinin anlayacağı gibi yaz."},
        "description": {"type": "string", "description": "Kısa ad, ör. 'sabah hava durumu'."},
        "catch_up": {"type": "boolean", "description": "Bilgisayar kapalıyken kaçırılırsa açılışta yap. Varsayılan true."},
        "notify": {"type": "boolean", "description": "Bitince sonucu bildir. Varsayılan true."},
    }, ["action"])
    risk = DANGER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") == "list" else (WRITE if args.get("action") == "remove" else DANGER)

    def summary(self, args: dict[str, Any]) -> str:
        if args.get("action") == "add" and args.get("cron"):
            from alice.core.schedule import Cron

            try:
                when = Cron(args["cron"]).describe()
            except ValueError:
                when = args["cron"]
            return f"{when}: {args.get('description') or args.get('prompt', '')[:120]}"
        return super().summary(args)

    def run(self, ctx: ToolContext, action: str, id: str | None = None, cron: str | None = None,
            prompt: str | None = None, description: str = "", catch_up: bool = True, notify: bool = True) -> ToolResult:
        from alice.core.schedule import ScheduledTask, TaskStore

        ts = TaskStore()
        if action == "list":
            return ToolResult("\n".join(t.summary() for t in ts.tasks) or "zamanlanmış görev yok")
        if action == "add":
            if not cron or not prompt:
                raise ToolError("add için cron ve prompt gerekli")
            try:
                task = ScheduledTask(cron=cron, prompt=prompt, description=description, catch_up=catch_up,
                                     notify=notify, created=time.time())
            except ValueError as e:
                raise ToolError(str(e)) from e
            ts.add(task)
            from alice.core.service import read_state

            warn = "" if read_state().get("running") else \
                "\nUyarı: çekirdek çalışmıyor; görevin çalışması için `alice core install` ya da `alice core run`."
            return ToolResult(f"görev kuruldu: {task.summary()}{warn}")
        if not id:
            raise ToolError("id gerekli")
        if action == "remove":
            return ToolResult("silindi" if ts.remove(id) else f"görev yok: {id}")
        task = ts.get(id)
        if task is None:
            raise ToolError(f"görev yok: {id}")
        task.pending = True
        ts.save()
        return ToolResult("bir sonraki çekirdek turunda çalışacak")


def _obj_arg(name: str, v: Any) -> dict[str, Any]:
    """Nesne parametreleri JSON metni olarak gelir (bazı sağlayıcılar serbest nesne kabul etmiyor)."""
    import json

    if v in (None, ""):
        return {}
    if isinstance(v, dict):
        return v
    try:
        out = json.loads(str(v))
    except ValueError as e:
        raise ToolError(f"{name} geçerli JSON değil: {e}") from e
    if not isinstance(out, dict):
        raise ToolError(f"{name} bir JSON nesnesi olmalı, ör. {{\"any\": [\"hayat\"]}}")
    return out


class Feeds(Tool):
    name = "feeds"
    description = (
        "Bağlı servisleri arka planda izleyip önemli bir şey olunca haber veren akışlar: 'Discord genel kanalında "
        "bana mention gelirse söyle', 'Ahmet Steam'de oyuna girince haber ver', 'DCS kanalına video gelince "
        "söyle', 'gece rahatsız etme, sabah özet geç'. Ölçüm eşiği (GPU sıcaklığı) için rules, belirli saat için "
        "schedule.\n"
        "- Önce connectors: hangi bağlantı hazır, eylemleri ve alanları ne.\n"
        "- add öncesi test_action ile örnek öğeleri gör; title/body şablonunda ve when koşulunda alan adlarını "
        "oradan al ({author.username} gibi iç içe yazılır).\n"
        "- Yeni öğe için mode=new; bir alanın değişmesi için (oyuna girdi, çevrimiçi oldu) mode=change + watch.\n"
        "- Gereksiz bildirimi önle: when ile süz, quiet ile geceyi sustur, acil olanı escalate ile işaretle."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "connectors", "test_action", "add", "test", "remove",
                                              "enable", "disable"],
                   "description": "list: akışlar | connectors: bağlantılar ve eylemleri | test_action: bir "
                   "bağlantı eylemini şimdi çağır, örnek öğeleri göster | add: yeni akış | test: tanımlı akışı "
                   "yokla, her öğe geçti mi neden | remove/enable/disable: name'li akış."},
        "name": {"type": "string", "description": "Akış adı (add, test, remove, enable, disable), ör. 'ahmet_oyun'."},
        "connector": {"type": "string", "description": "Bağlantı adı, ör. 'discord', 'steam', 'youtube_api'."},
        "connector_action": {"type": "string", "description": "Bağlantının eylemi, ör. 'messages', 'profile'."},
        "args": {"type": "string", "description": "Eylem parametreleri JSON metni, ör. '{\"channel_id\": \"123\"}'."},
        "every_minutes": {"type": "number", "description": "Yoklama aralığı, dakika. Varsayılan 10, en az 1."},
        "mode": {"type": "string", "enum": ["new", "change"],
                 "description": "new: listeye yeni giren öğe | change: watch'taki alan değişince."},
        "watch": {"type": "array", "items": {"type": "string", "description": "Alan adı."},
                  "description": "mode=change için izlenecek alanlar, ör. [\"gameextrainfo\"]."},
        "id_field": {"type": "string", "description": "Öğeyi ayırt eden alan. Varsayılan 'id' (Steam'de 'steamid')."},
        "title": {"type": "string", "description": "Bildirim başlığı şablonu, ör. '{personaname} oyuna girdi'."},
        "body": {"type": "string", "description": "Bildirim gövdesi şablonu, ör. '{content}'. change modunda "
                 "{prev.alan} eski değer, {changed} değişen alanlar."},
        "when": {"type": "string", "description": "Hangi öğe bildirime değer, JSON: any/all/none (kelime listesi), "
                 "regex, fields (alan -> değer ya da liste, \"*\" boş değil), min/max (sayısal), ask (anlama göre "
                 "evet/hayır sorusu). Ör. '{\"any\": [\"hayat\"], \"none\": [\"bot\"]}'."},
        "escalate": {"type": "string", "description": "Bu koşul tutarsa önem critical (sessiz saatte de söylenir), "
                     "when ile aynı biçim."},
        "quiet": {"type": "string", "description": "Biriktirilecek saatler, ör. '23:00-08:00'; bitince özet gelir."},
        "digest_minutes": {"type": "number", "description": "Tek tek değil bu aralıkla özet. 0: kapalı."},
        "cooldown_minutes": {"type": "number", "description": "Bir bildirimden sonra gelenleri bu süre topla."},
        "severity": {"type": "string", "enum": ["info", "warn", "critical"], "description": "Önem. Varsayılan info."},
        "agent": {"type": "string", "description": "Doluysa bildirim gidince ALICE uyanıp bu işi yapar (özetle, "
                  "kontrol et). Gözetimsiz çalıştığı için onay ister."},
        "description": {"type": "string", "description": "Kullanıcının kendi cümlesi (kayıtta not olarak durur)."},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a in ("list", "connectors"):
            return READ
        if a in ("test", "test_action"):
            return NETWORK
        return DANGER if a == "add" and args.get("agent") else WRITE

    def summary(self, args: dict[str, Any]) -> str:
        a = args.get("action", "")
        if a in ("add", "test", "remove", "enable", "disable"):
            return f"{a} {args.get('name', '')}".strip()
        if a == "test_action":
            return f"{args.get('connector', '')}.{args.get('connector_action', '')}"
        return a

    def run(self, ctx: ToolContext, action: str, name: str = "", connector: str = "", connector_action: str = "",
            args: Any = None, **fields: Any) -> ToolResult:
        from alice.core import feedfile
        from alice.core.feeds import FeedMonitor, load_feeds

        if action == "list":
            feeds, errors = load_feeds()
            lines = [f"{f.name}{'' if f.enabled else ' (kapalı)'}: {f.describe()}" for f in feeds]
            return ToolResult("\n".join(lines + [f"hata: {e}" for e in errors]) or "akış yok")
        if action == "connectors":
            return ToolResult(_connectors_text(ctx))
        if action == "test_action":
            return ToolResult(_test_action(ctx, connector, connector_action, _obj_arg("args", args)))
        if action in ("remove", "enable", "disable", "test") and not name:
            raise ToolError(f"{action} için name gerekli")
        if action == "remove":
            return ToolResult("silindi" if feedfile.remove_feed(name) else f"akış yok: {name}")
        if action in ("enable", "disable"):
            ok = feedfile.set_enabled(name, action == "enable")
            return ToolResult(("açıldı" if action == "enable" else "kapatıldı") if ok else f"akış yok: {name}")
        if action == "test":
            feed = next((f for f in load_feeds()[0] if f.name == name), None)
            if feed is None:
                raise ToolError(f"akış yok: {name}")
            try:
                verdicts = FeedMonitor(cfg=ctx.config, decider=ctx.decider).explain(feed)
            except Exception as e:  # noqa: BLE001 - bağlantı hatası modele anlaşılır dönsün
                raise ToolError(f"yoklanamadı: {e}") from e
            lines = [f"{'✓' if v.ok else '✗'} {v.title}" + (f" — {v.body}" if v.body else "")
                     + f"  ({v.reason}{', ' + v.severity if v.ok and v.severity != feed.severity else ''})"
                     for v in verdicts[:15]]
            return ToolResult(f"{sum(v.ok for v in verdicts)}/{len(verdicts)} öğe geçiyor\n" + "\n".join(lines))
        if action == "add":
            return ToolResult(_add(ctx, name, connector, connector_action, _obj_arg("args", args), fields))
        raise ToolError(f"bilinmeyen işlem: {action}")


def _connectors_text(ctx: ToolContext) -> str:
    from alice.connect.manifest import discover
    from alice.connect.runner import ready

    out = []
    for m in sorted(discover(ctx.config)[0].values(), key=lambda x: x.name):
        ok, note = ready(m)
        out.append(f"{m.name} ({'hazır' if ok else note})")
        for a in m.actions.values():
            if a.writes or a.sends:
                continue  # akış sadece okuyan eylemleri yoklar
            params = ", ".join(f"{p.name}{'*' if p.required else ''}" for p in a.params.values())
            fields = f" alanlar: {', '.join(a.fields)}" if a.fields else ""
            out.append(f"  - {a.name}({params}): {a.summary}{fields}")
    return "\n".join(out) or "bağlantı yok"


def _test_action(ctx: ToolContext, connector: str, action: str, args: dict[str, Any]) -> str:
    import json

    from alice.connect.manifest import discover
    from alice.connect.runner import ConnectError, call, pick, ready

    m = discover(ctx.config)[0].get(connector)
    if m is None:
        raise ToolError(f"bağlantı yok: {connector}")
    act = m.actions.get(action)
    if act is None:
        raise ToolError(f"{connector}: eylem yok: {action}. Olanlar: {', '.join(sorted(m.actions))}")
    if act.writes or act.sends:
        raise ToolError(f"{connector}.{action} bir şey değiştiriyor/gönderiyor; akış sadece okuyan eylemleri yoklar")
    ok, note = ready(m)
    if not ok:
        raise ToolError(f"{m.label} hazır değil: {note}")
    try:
        data = call(m, act, args)
    except ConnectError as e:
        raise ToolError(str(e)) from e
    rows = pick(data, act.pick) if act.pick else data
    rows = rows if isinstance(rows, list) else [rows]
    sample = json.dumps(rows[:3], ensure_ascii=False, indent=1)
    return f"{len(rows)} öğe. İlk {min(3, len(rows))} tanesi (alan adlarını buradan al):\n{sample[:6000]}"


def _add(ctx: ToolContext, name: str, connector: str, action: str, args: dict[str, Any],
         fields: dict[str, Any]) -> str:
    from alice.connect.manifest import discover
    from alice.connect.runner import ready
    from alice.core import feedfile

    if not (name and connector and action):
        raise ToolError("add için name, connector ve connector_action gerekli")
    m = discover(ctx.config)[0].get(connector)
    if m is None:
        raise ToolError(f"bağlantı yok: {connector}. feeds action=connectors ile bak")
    act = m.actions.get(action)
    if act is None:
        raise ToolError(f"{connector}: eylem yok: {action}. Olanlar: {', '.join(sorted(m.actions))}")
    if act.writes or act.sends:
        raise ToolError(f"{connector}.{action} bir şey değiştiriyor/gönderiyor; akışa okuyan bir eylem ver")
    missing = [p.name for p in act.params.values() if p.required and not str(args.get(p.name, "")).strip()]
    if missing:
        raise ToolError(f"{connector}.{action} için args içinde eksik: {', '.join(missing)}")
    feed: dict[str, Any] = {"name": name, "connector": connector, "action": action, "args": args}
    for k in ("every_minutes", "mode", "watch", "id_field", "title", "body", "quiet", "digest_minutes",
              "cooldown_minutes", "severity", "agent", "description"):
        if fields.get(k) not in (None, "", []):
            feed[k] = fields[k]
    for sub in ("when", "escalate"):
        tab = _obj_arg(sub, fields.get(sub))
        if tab:
            feed[sub] = tab
    try:
        feedfile.add_feed(feed)
    except ValueError as e:
        raise ToolError(str(e)) from e
    hazır = "" if ready(m)[0] else f" Not: {m.label} henüz hazır değil, anahtar eklenince çalışır."
    return (f"akış eklendi: {name}. Çekirdek çalışırken {feed.get('every_minutes', 10)} dakikada bir yoklanır; "
            f"ilk tur sessizdir (mevcut öğeler görüldü sayılır).{hazır} Denemek için: feeds action=test name={name}")


class ActionLog(Tool):
    name = "action_log"
    parallel_safe = True
    description = ("ALICE'in kullanıcı adına başkasına gönderdiği şeylerin kaydı (Discord/WhatsApp mesajı, DM, "
                   "e-posta): gönderilen, kullanıcının reddettiği, plan modunda engellenen ve başarısız olan. "
                   "'Bugün kime ne yazdın?', 'benim adıma ne gönderdin?' sorularında kullan.")
    parameters = obj({
        "limit": {"type": "integer", "description": "En fazla kaç kayıt. Varsayılan 20."},
        "hours": {"type": "number", "description": "Sadece son bu kadar saat. Boş: hepsi."},
    })
    risk = READ

    def run(self, ctx: ToolContext, limit: int = 20, hours: float | None = None) -> ToolResult:
        from alice import actionlog

        since = time.time() - float(hours) * 3600 if hours else 0.0
        rows = actionlog.recent(max(1, min(int(limit), 200)), since)
        return ToolResult("\n".join(actionlog.format_row(r) for r in rows) or "kayıt yok: kimseye bir şey gönderilmedi")


TOOLS = [Metrics(), Rules(), Schedule(), Feeds(), ActionLog()]
