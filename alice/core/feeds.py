"""Akışlar: bağlı servisleri arka planda izle, yeni bir şey olunca ALICE söylesin.

Jarvis'in "sorulunca cevap veren" değil "olanı haber veren" tarafı bu. Çekirdek servis her turda
vadesi gelen akışları yokluyor: bağlantının (alice/connect) bir eylemini çağırıyor, dönen listedeki
öğeleri daha önce görülenlerle karşılaştırıyor, yeni olanları bildirime çeviriyor.

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
    metric = "feed.discord.yeni"     # opsiyonel: sayı metrik olarak da yazılır, kurallar kullanabilir
    agent = ""                        # dolu: yeni öğe gelince ALICE uyanır ve bu işi yapar

İlk turda hiçbir şey bildirilmez: o anki öğeler "görüldü" diye işaretlenir. Yoksa akışı ilk kurduğunda
son 15 mesaj peş peşe bildirim olarak gelirdi.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from alice.config import alice_home
from alice.core.notify import Notification

FIELD_RX = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")
MAX_SEEN = 200          # akış başına hatırlanan öğe kimliği
MAX_PER_TICK = 5        # bir turda en fazla bu kadar bildirim (akış patlarsa ekranı doldurmasın)


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

    @property
    def interval(self) -> float:
        return max(60.0, float(self.every_minutes) * 60)


def feeds_path() -> Path:
    return alice_home() / "feeds.toml"


def state_path() -> Path:
    return alice_home() / "feeds-state.json"


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
        name = str(raw.get("name", "")).strip()
        if not name or not raw.get("connector") or not raw.get("action"):
            errors.append(f"akış atlandı: name, connector ve action zorunlu ({raw})")
            continue
        out.append(Feed(name=name, connector=str(raw["connector"]), action=str(raw["action"]),
                        args=dict(raw.get("args") or {}), every_minutes=float(raw.get("every_minutes", 10) or 10),
                        id_field=str(raw.get("id_field", "id")), title=str(raw.get("title", name)),
                        body=str(raw.get("body", "")), severity=str(raw.get("severity", "info")),
                        topic=str(raw.get("topic", "")), metric=str(raw.get("metric", "")),
                        agent=str(raw.get("agent", "")), enabled=bool(raw.get("enabled", True))))
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


class FeedMonitor:
    """Çekirdek servise takılan gözlemci: akışları yoklar, yeni öğeleri bildirir, sayıyı metrik yazar."""

    name = "feeds"
    processes: set[str] = set()

    def __init__(self, notify: Callable[[Notification], Any] | None = None,
                 run_agent: Callable[[str], str] | None = None, cfg: Any = None,
                 fetch: Callable[[Feed], list[Any]] | None = None, emit: Callable[[str], None] | None = None):
        self.notify = notify
        self.run_agent = run_agent
        self.cfg = cfg
        self._fetch = fetch or self._call_connector
        self.emit = emit or (lambda _s: None)
        self.feeds, self.errors = load_feeds()
        self.state = _read_state()

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
        data = call(m, act, feed.args)
        rows = pick(data, act.pick) if act.pick else data
        if isinstance(rows, dict):
            rows = [rows]
        return list(rows or [])

    def _item_id(self, feed: Feed, item: Any) -> str:
        from alice.connect.runner import pick

        v = pick(item, feed.id_field) if isinstance(item, dict) else None
        return str(v) if v not in (None, "") else json.dumps(item, ensure_ascii=False, sort_keys=True)[:200]

    # ------------------------------------------------------------------ tur
    def due(self, feed: Feed, now: float) -> bool:
        last = float((self.state.get(feed.name) or {}).get("last") or 0)
        return now - last >= feed.interval

    def collect(self) -> list[tuple[str, float | None, str | None]]:
        now = time.time()
        rows: list[tuple[str, float | None, str | None]] = []
        changed = False
        for feed in self.feeds:
            if not feed.enabled or not self.due(feed, now):
                continue
            row = dict(self.state.get(feed.name) or {})
            row["last"] = now
            self.state[feed.name] = row
            changed = True
            try:
                items = self._fetch(feed)
            except Exception as e:  # noqa: BLE001 - bir akış patlasa diğerleri sürsün
                row["error"] = str(e)[:200]
                self.emit(f"akış {feed.name}: {e}")
                continue
            row.pop("error", None)
            seen = list(row.get("seen") or [])
            first_run = "seen" not in row
            ids = [self._item_id(feed, it) for it in items]
            new = [(i, it) for i, it in zip(ids, items) if i not in set(seen)]
            row["seen"] = (seen + ids)[-MAX_SEEN:]
            if feed.metric:
                rows.append((feed.metric, float(len(new)), None))
            if first_run:
                self.emit(f"akış {feed.name}: {len(items)} öğe görüldü (ilk tur, bildirim yok)")
                continue
            self._announce(feed, [it for _i, it in new])
        if changed:
            _write_state(self.state)
        return rows

    def _announce(self, feed: Feed, new: list[Any]) -> None:
        if not new:
            return
        shown = new[:MAX_PER_TICK]
        for item in shown:
            if self.notify is not None:
                self.notify(Notification(title=fill(feed.title, item, feed) or feed.name,
                                         body=fill(feed.body, item, feed), severity=feed.severity,
                                         topic=feed.topic or feed.connector, kind="info"))
        if len(new) > len(shown) and self.notify is not None:
            self.notify(Notification(title=f"{feed.name}: {len(new) - len(shown)} yeni öğe daha",
                                     severity="info", topic=feed.topic or feed.connector, kind="info"))
        if feed.agent and self.run_agent is not None:
            özet = "\n".join(f"- {fill(feed.title, it, feed)} {fill(feed.body, it, feed)}".strip() for it in shown)
            try:
                self.run_agent(f"{feed.agent}\n\nYeni gelenler:\n{özet}")
            except Exception as e:  # noqa: BLE001 - ajan patlarsa akış ölmesin
                self.emit(f"akış {feed.name}: ajan çalıştırılamadı: {e}")
