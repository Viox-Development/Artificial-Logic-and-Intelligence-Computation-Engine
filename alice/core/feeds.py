"""Akışlar: bağlı servisleri arka planda izle, önemli bir şey olunca ALICE söylesin.

Jarvis'in "sorulunca cevap veren" değil "olanı haber veren" tarafı bu. Çekirdek servis her turda
vadesi gelen akışları yokluyor: bağlantının (alice/connect) bir eylemini çağırıyor, dönen öğeleri
daha önce görülenlerle karşılaştırıyor, koşulları geçenleri bildirime çeviriyor.

Tanım `~/.alice/feeds.toml`:

    [[feed]]
    name = "discord_genel"
    connector = "discord"
    action = "messages"
    args = { channel_id = "123456" }
    every_minutes = 5
    id_field = "id"
    title = "Discord · {author.username}"
    body = "{content}"
    topic = "discord"
    metric = "feed.discord.yeni"   # opsiyonel: koşulu geçen sayı metrik olur, kurallar kullanabilir
    agent = ""                      # dolu: bildirim gidince ALICE uyanır ve bu işi yapar

    [feed.when]                     # hangi öğe bildirime değer (bkz. alice/core/conditions.py)
    any = ["alice", "@everyone"]
    none = ["bot"]
    ask = "Bu mesaj bana bir şey soruyor ya da acil mi?"

    [feed.escalate]                 # bu koşul tutarsa önem "critical": sessiz saatte de söylenir
    regex = "\\b(acil|hemen)\\b"

İki çalışma şekli var:

* `mode = "new"` (varsayılan): listeye yeni giren öğe (yeni mesaj, yeni video).
* `mode = "change"`: aynı öğenin izlenen alanı değişince (`watch = ["gameextrainfo"]`), ör. "Ahmet DCS'e
  girdi". Şablonda `{prev.alan}` eski değeri, `{changed}` değişen alanları verir.

Teslim zamanlaması:

* `digest_minutes = 60`: tek tek değil, saatte bir özet halinde.
* `quiet = "23:00-08:00"`: bu saatlerde biriktirilir, saat bitince özet gelir (critical olan beklemez).
* `cooldown_minutes = 10`: bir bildirimden sonra bu kadar süre gelenler toplanır, sonra özet olarak gelir.

İlk turda hiçbir şey bildirilmez: o anki öğeler "görüldü" diye işaretlenir. Yoksa akışı ilk kurduğunda
son 15 mesaj peş peşe bildirim olarak gelirdi.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from alice.config import alice_home
from alice.core.conditions import Condition, check, haystack, parse_condition
from alice.core.notify import Notification

FIELD_RX = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")
QUIET_RX = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
# Şablondan kalmış, doldurulmamış değer ("KANAL_ID", "STEAMID1,STEAMID2")
_PLACEHOLDER = re.compile(r"^(?=.*[_0-9])[A-Z][A-Z0-9_]{3,}(,[A-Z][A-Z0-9_]{3,})*$")
MAX_SEEN = 200          # akış başına hatırlanan öğe kimliği / izlenen öğe sayısı
MAX_PER_TICK = 5        # bir turda en fazla bu kadar tek tek bildirim (akış patlarsa ekranı doldurmasın)
MAX_HELD = 50           # özet için biriktirilen en fazla öğe
DIGEST_LINES = 6        # özet bildiriminde gösterilen satır
MODES = ("new", "change")


@dataclass
class Feed:
    name: str
    connector: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    every_minutes: float = 10.0
    id_field: str = "id"
    title: str = "{name}"
    body: str = ""
    severity: str = "info"
    topic: str = ""
    metric: str = ""
    agent: str = ""
    enabled: bool = True
    mode: str = "new"
    watch: list[str] = field(default_factory=list)
    when: Condition = field(default_factory=Condition)
    escalate: Condition = field(default_factory=Condition)
    escalate_severity: str = "critical"
    digest_minutes: float = 0.0
    quiet: str = ""
    cooldown_minutes: float = 0.0

    @property
    def interval(self) -> float:
        return max(60.0, float(self.every_minutes) * 60)

    def describe(self) -> str:
        bits = [f"{self.connector}.{self.action}", f"{self.every_minutes:g} dk"]
        if self.mode == "change":
            bits.append("değişince: " + ", ".join(self.watch))
        if not self.when.empty():
            bits.append("koşul: " + self.when.summary())
        if not self.escalate.empty():
            bits.append("acil: " + self.escalate.summary())
        if self.digest_minutes:
            bits.append(f"{self.digest_minutes:g} dk'da bir özet")
        if self.quiet:
            bits.append(f"sessiz {self.quiet}")
        if self.cooldown_minutes:
            bits.append(f"bekleme {self.cooldown_minutes:g} dk")
        if self.agent:
            bits.append("ALICE'i uyandırır")
        return " · ".join(bits)


def feeds_path() -> Path:
    return alice_home() / "feeds.toml"


def state_path() -> Path:
    return alice_home() / "feeds-state.json"


def _parse_quiet(s: str) -> tuple[int, int] | None:
    """'23:00-08:00' -> (1380, 480) dakika. Boşsa None, bozuksa ValueError."""
    if not s:
        return None
    m = QUIET_RX.match(s)
    if not m:
        raise ValueError(f"quiet 'SS:DD-SS:DD' biçiminde olmalı: {s!r}")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60):
        raise ValueError(f"quiet saatleri geçersiz: {s!r}")
    return h1 * 60 + m1, h2 * 60 + m2


def in_quiet(feed: Feed, now: float) -> bool:
    q = _parse_quiet(feed.quiet)
    if q is None:
        return False
    start, end = q
    t = datetime.fromtimestamp(now)
    cur = t.hour * 60 + t.minute
    return start <= cur < end if start <= end else (cur >= start or cur < end)  # gece yarısını geçen aralık


def parse_feed(raw: dict[str, Any]) -> Feed:
    name = str(raw.get("name", "")).strip()
    if not name or not raw.get("connector") or not raw.get("action"):
        raise ValueError(f"akış atlandı: name, connector ve action zorunlu ({raw.get('name') or raw})")
    mode = str(raw.get("mode", "new"))
    if mode not in MODES:
        raise ValueError(f"{name}: mode new ya da change olmalı")
    watch = [str(x) for x in raw.get("watch", [])]
    if mode == "change" and not watch:
        raise ValueError(f"{name}: mode = \"change\" için watch = [\"alan\", ...] gerekir")
    quiet = str(raw.get("quiet", ""))
    _parse_quiet(quiet)  # bozuksa burada söylensin, gece 3'te değil
    esc = raw.get("escalate") or {}
    esc = dict(esc) if isinstance(esc, dict) else esc  # severity koşul alanı değil: ayır, girdiyi bozma
    esc_sev = str(esc.pop("severity", "critical")) if isinstance(esc, dict) else "critical"
    return Feed(name=name, connector=str(raw["connector"]), action=str(raw["action"]),
                args=dict(raw.get("args") or {}), every_minutes=float(raw.get("every_minutes", 10) or 10),
                id_field=str(raw.get("id_field", "id")), title=str(raw.get("title", name)),
                body=str(raw.get("body", "")), severity=str(raw.get("severity", "info")),
                topic=str(raw.get("topic", "")), metric=str(raw.get("metric", "")),
                agent=str(raw.get("agent", "")), enabled=bool(raw.get("enabled", True)), mode=mode, watch=watch,
                when=parse_condition(raw.get("when"), f"{name}.when"),
                escalate=parse_condition(esc, f"{name}.escalate"), escalate_severity=esc_sev,
                digest_minutes=float(raw.get("digest_minutes", 0) or 0), quiet=quiet,
                cooldown_minutes=float(raw.get("cooldown_minutes", 0) or 0))


def load_feeds(path: Path | None = None) -> tuple[list[Feed], list[str]]:
    """(akışlar, hatalar). Bozuk tanım diğerlerini düşürmez."""
    p = path or feeds_path()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except OSError:
        return [], []
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return [], [f"{p.name}: okunamadı: {e}"]
    out: list[Feed] = []
    errors: list[str] = []
    for raw in data.get("feed") or []:
        if not isinstance(raw, dict):
            continue
        blank = [k for k, v in (raw.get("args") or {}).items() if isinstance(v, str) and _PLACEHOLDER.match(v)]
        if blank:
            # Şablondaki "KANAL_ID" gibi doldurulmamış değer: akış hiç kurulmamış, kapalı sayılır (listede ve
            # panelde görünür ama yoklanmaz). Eskiden her 5 dakikada çalışıp aynı hatayı veriyordu ("ornek" akışı).
            errors.append(f"akış kapalı: {raw.get('name', '?')}: {', '.join(blank)} doldurulmamış")
            raw = {**raw, "enabled": False}
        try:
            out.append(parse_feed(dict(raw)))
        except ValueError as e:
            msg = str(e)
            errors.append(msg if msg.startswith("akış atlandı") else f"akış atlandı: {msg}")
    return out, errors


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(data: dict[str, Any]) -> None:
    try:
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def fill(template: str, item: Any, feed: Feed) -> str:
    """{alan} ve {iç.içe.alan} yer tutucularını doldur; {name} akışın adı."""
    from alice.connect.runner import pick

    def one(m: re.Match) -> str:
        key = m.group(1)
        if key == "name":
            return feed.name
        v = pick(item, key) if isinstance(item, (dict, list)) else None
        return "" if v is None else str(v)

    return " ".join(FIELD_RX.sub(one, template).split())


@dataclass
class Note:
    """Koşulları geçmiş, teslim edilmeyi bekleyen bir öğe."""
    title: str
    body: str
    severity: str
    item: Any = None

    def line(self) -> str:
        return f"{self.title} — {self.body}" if self.body else self.title


@dataclass
class Verdict:
    """`alice feeds --test` için: öğe geçti mi, neden, hangi önemle."""
    item: Any
    title: str
    body: str
    ok: bool
    reason: str
    severity: str


class FeedMonitor:
    """Çekirdek servise takılan gözlemci: akışları yoklar, koşulları geçenleri bildirir, sayıyı metrik yazar."""

    name = "feeds"
    processes: set[str] = set()

    def __init__(self, notify: Callable[[Notification], Any] | None = None,
                 run_agent: Callable[[str], str] | None = None, cfg: Any = None,
                 fetch: Callable[[Feed], list[Any]] | None = None, emit: Callable[[str], None] | None = None,
                 decider: Any = None, clock: Callable[[], float] | None = None):
        self.notify = notify
        self.run_agent = run_agent
        self.cfg = cfg
        self.decider = decider
        self._fetch = fetch or self._call_connector
        self.emit = emit or (lambda _s: None)
        self.clock = clock or time.time
        self.feeds, self.errors = load_feeds()
        self._mtime = self._file_mtime()
        self.state = _read_state()

    @staticmethod
    def _file_mtime() -> float:
        try:
            return feeds_path().stat().st_mtime
        except OSError:
            return 0.0

    def reload_if_changed(self) -> bool:
        """feeds.toml değiştiyse (sohbetten akış eklendi, elle düzenlendi) yeniden yükle; durum korunur."""
        mt = self._file_mtime()
        if mt == self._mtime:
            return False
        self._mtime = mt
        self.feeds, self.errors = load_feeds()
        for e in self.errors:
            self.emit(e)
        return True

    # ------------------------------------------------------------------ veri
    def _call_connector(self, feed: Feed) -> list[Any]:
        from alice.connect.manifest import discover
        from alice.connect.runner import ConnectError, call, pick, ready

        manifests, _ = discover(self.cfg)
        m = manifests.get(feed.connector)
        if m is None:
            raise ConnectError(f"bağlantı yok: {feed.connector}")
        act = m.actions.get(feed.action)
        if act is None:
            raise ConnectError(f"{feed.connector}: eylem yok: {feed.action}")
        hazır, sebep = ready(m)
        if not hazır:
            raise ConnectError(f"{m.label}: {sebep}")
        data = call(m, act, feed.args, cache=False)  # akış hep taze ister: önbellek yeni mesajı saklamasın
        rows = pick(data, act.pick) if act.pick else data
        if isinstance(rows, dict):
            rows = [rows]
        return list(rows or [])

    def _item_id(self, feed: Feed, item: Any) -> str:
        from alice.connect.runner import pick

        v = pick(item, feed.id_field) if isinstance(item, dict) else None
        return str(v) if v not in (None, "") else json.dumps(item, ensure_ascii=False, sort_keys=True)[:200]

    def _ask(self, feed: Feed) -> Callable[[str, str], bool] | None:
        """Koşuldaki anlam sorusu için karar motoru (Jev / hızlı model), kurallardaki confirm gibi."""
        if self.decider is None:
            return None

        def ask(question: str, text: str) -> bool:
            from alice.decider import Noul

            state = f"Feed '{feed.name}' ({feed.connector}.{feed.action}). New item:\n{text[:1500]}"
            d = self.decider.decide(state, {"notify": Noul(question)})
            return float(d["notify"].value) >= 0.5

        return ask

    def judge(self, feed: Feed, item: Any) -> Verdict:
        """Öğe bildirime değer mi, değerse hangi önemle? Durumu değiştirmez (test komutu da bunu kullanır)."""
        title = fill(feed.title, item, feed) or feed.name
        body = fill(feed.body, item, feed)
        text = haystack(item, title, body)
        ok, reason = check(feed.when, item, text, self._ask(feed))
        severity = feed.severity
        if ok and not feed.escalate.empty():
            hot, _why = check(feed.escalate, item, text, self._ask(feed))
            if hot:
                severity, reason = feed.escalate_severity, "geçti (acil)"
        return Verdict(item, title, body, ok, reason, severity)

    # ------------------------------------------------------------------ tur
    def due(self, feed: Feed, now: float) -> bool:
        last = float((self.state.get(feed.name) or {}).get("last") or 0)
        return now - last >= feed.interval

    def collect(self) -> list[tuple[str, float | None, str | None]]:
        self.reload_if_changed()
        now = self.clock()
        rows: list[tuple[str, float | None, str | None]] = []
        changed = False
        for feed in self.feeds:
            if not feed.enabled:
                continue
            row = self.state.setdefault(feed.name, {})
            if self.due(feed, now):
                row["last"] = now
                changed = True
                try:
                    items = self._fetch(feed)
                except Exception as e:  # noqa: BLE001 - bir akış patlasa diğerleri sürsün
                    # aynı hata her turda yazılmasın (kurulmamış örnek akış 2 günde 74 kez "token yok" yazdı)
                    if row.get("error") != str(e)[:200]:
                        self.emit(f"akış {feed.name}: {e}")
                    row["error"] = str(e)[:200]
                    items = None
                if items is not None:
                    row.pop("error", None)
                    fresh = self._changes(feed, row, items) if feed.mode == "change" else self._new(feed, row, items)
                    if fresh is None and feed.metric:
                        rows.append((feed.metric, 0.0, None))  # ilk tur: hiçbiri "yeni" değil
                    if fresh is not None:
                        notes = [Note(v.title, v.body, v.severity, v.item)
                                 for v in (self.judge(feed, it) for it in fresh) if v.ok]
                        if feed.metric:
                            rows.append((feed.metric, float(len(notes)), None))
                        self._deliver(feed, row, notes, now)
            # Süresi dolan özet (sessiz saat bitti, bekleme geçti) yoklama zamanı gelmese de gitsin.
            if row.get("held") and self._flush_due(feed, row, now):
                self._flush(feed, row, now)
                changed = True
        if changed:
            _write_state(self.state)
        return rows

    def _new(self, feed: Feed, row: dict[str, Any], items: list[Any]) -> list[Any] | None:
        """Listeye yeni giren öğeler. İlk turda None (sadece işaretlenir)."""
        seen = list(row.get("seen") or [])
        first_run = "seen" not in row
        ids = [self._item_id(feed, it) for it in items]
        known = set(seen)
        new = [it for i, it in zip(ids, items) if i not in known]
        row["seen"] = (seen + [i for i in ids if i not in known])[-MAX_SEEN:]
        if first_run:
            self.emit(f"akış {feed.name}: {len(items)} öğe görüldü (ilk tur, bildirim yok)")
            return None
        return new

    def _changes(self, feed: Feed, row: dict[str, Any], items: list[Any]) -> list[Any] | None:
        """İzlenen alanı değişen öğeler; şablon için {prev.alan} ve {changed} eklenir."""
        from alice.connect.runner import pick

        snap: dict[str, dict[str, Any]] = dict(row.get("snap") or {})
        first_run = "snap" not in row
        out = []
        for it in items:
            key = self._item_id(feed, it)
            cur = {f: pick(it, f) if isinstance(it, (dict, list)) else None for f in feed.watch}
            prev = snap.get(key)
            if prev is not None and prev != cur:
                diff = [f for f in feed.watch if prev.get(f) != cur.get(f)]
                base = it if isinstance(it, dict) else {"value": it}
                out.append({**base, "prev": prev, "changed": ", ".join(diff)})
            snap[key] = cur
        if len(snap) > MAX_SEEN:
            snap = dict(list(snap.items())[-MAX_SEEN:])
        row["snap"] = snap
        if first_run:
            self.emit(f"akış {feed.name}: {len(items)} öğe izlenmeye başladı (ilk tur, bildirim yok)")
            return None
        return out

    # ------------------------------------------------------------------ teslim
    def _cooling(self, feed: Feed, row: dict[str, Any], now: float) -> bool:
        return bool(feed.cooldown_minutes) and now - float(row.get("last_sent") or 0) < feed.cooldown_minutes * 60

    def _deliver(self, feed: Feed, row: dict[str, Any], notes: list[Note], now: float) -> None:
        if not notes:
            return
        quiet = in_quiet(feed, now)
        urgent = [n for n in notes if n.severity == "critical"]
        rest = [n for n in notes if n.severity != "critical"]
        hold = bool(feed.digest_minutes) or quiet or self._cooling(feed, row, now)
        now_list = urgent + ([] if hold else rest)
        if hold and rest:
            held = list(row.get("held") or [])
            held += [{"title": n.title, "body": n.body, "severity": n.severity} for n in rest]
            row["held"] = held[-MAX_HELD:]
            row.setdefault("held_since", now)
        if now_list:
            self._send(feed, now_list)
            row["last_sent"] = now

    def _flush_due(self, feed: Feed, row: dict[str, Any], now: float) -> bool:
        if in_quiet(feed, now):
            return False
        if feed.digest_minutes:
            return now - float(row.get("last_flush") or row.get("held_since") or now) >= feed.digest_minutes * 60
        return not self._cooling(feed, row, now)

    def _flush(self, feed: Feed, row: dict[str, Any], now: float) -> None:
        held = [Note(h.get("title", ""), h.get("body", ""), h.get("severity", "info")) for h in row.get("held") or []]
        row["held"] = []
        row.pop("held_since", None)
        row["last_flush"] = row["last_sent"] = now
        if not held:
            return
        if self.notify is not None:
            lines = [n.line() for n in held[:DIGEST_LINES]]
            more = len(held) - len(lines)
            body = "\n".join(lines) + (f"\n… ve {more} tane daha" if more > 0 else "")
            self.notify(Notification(title=f"{feed.name}: {len(held)} yeni", body=body, severity="info",
                                     topic=feed.topic or feed.connector, kind="info"))
        self._wake_agent(feed, held)

    def _send(self, feed: Feed, notes: list[Note]) -> None:
        shown = notes[:MAX_PER_TICK]
        if self.notify is not None:
            for n in shown:
                self.notify(Notification(title=n.title, body=n.body, severity=n.severity,
                                         topic=feed.topic or feed.connector, kind="info"))
            if len(notes) > len(shown):
                self.notify(Notification(title=f"{feed.name}: {len(notes) - len(shown)} yeni öğe daha",
                                         severity="info", topic=feed.topic or feed.connector, kind="info"))
        self._wake_agent(feed, shown)

    def _wake_agent(self, feed: Feed, notes: list[Note]) -> None:
        if not feed.agent or self.run_agent is None or not notes:
            return
        özet = "\n".join(f"- {n.line()}" for n in notes)
        try:
            self.run_agent(f"{feed.agent}\n\nYeni gelenler:\n{özet}")
        except Exception as e:  # noqa: BLE001 - ajan patlarsa akış ölmesin
            self.emit(f"akış {feed.name}: ajan çalıştırılamadı: {e}")

    # ------------------------------------------------------------------ test komutu
    def explain(self, feed: Feed) -> list[Verdict]:
        """Şimdi yokla ve her öğe için koşulun ne dediğini döndür (durum değişmez, bildirim gitmez)."""
        return [self.judge(feed, it) for it in self._fetch(feed)]
