"""Projeler: büyük bir işi paralel çalışan ajanlarla, kendi yöneticisi olan otonom bir proje olarak yürütmek.

Her projenin bir yöneticisi (lead, adı "alice") ve işçi ajanları (üyeler) vardır. Hepsi kendi thread'inde, kendi
bağlamıyla çalışan alt ajanlardır.

Yönetici sohbetten bağımsız çalışır ve olay geldikçe uyanır:
- Plan: ajan verilmemişse kodu inceleyip işi parçalara böler, `assign` ile ajanları kurar.
- İzleme: ajanların sorularını `message` ile cevaplar; kullanıcıya bağlı kararları `ask_user` ile sorar.
- İnceleme: biten her ajan "review" durumuna geçer; yönetici raporu ve farkı (`diff`) okur, gerekirse test çalıştırır,
  `review` ile onaylar (worktree dalı birleştirilir) ya da geri bildirimle geri gönderir.
- Bitiş: herkes onaylanınca bütünü doğrular ve `finish` ile kullanıcıya özet yazar.

İnceleme kurulu (alice/council.py) açıksa: plan `plan` aracıyla kurula sunulur (reddedilirse yönetici düzeltip
tekrar sunar, ikinci retten sonra kullanıcıya sorulur); biten her iş kurulda tartışılıp oylanır ve karar uygulanır
(onay ya da doğrulanmış bulgularla düzeltme; düzeltmeden sonra kısa kontrol turu, en çok 2 kez); proje sonunda final
kurulu toplanır (en çok 2 kez). Çekişmeli kararlar yöneticiye, kullanıcı da oy verebilir ya da kararı bozabilir.

Üyeler `team_send` ile birbirine, yöneticiye ("alice") ya da herkese yazar, `team_wait` ile cevap bekler; gelen mesajlar
her araç adımından sonra konuşmalarına eklenir. `after` ile bir üye başkalarının işi onaylanınca başlar.

Her proje ~/.alice/projects/<id>.json'a kaydedilir; uygulama kapanıp açılsa da yüklenir, kullanıcı yazınca kaldığı
yerden devam eder. Uygulamanın Projeler sayfası durumları, konuşmaları, farkları ve soruları gösterir.

Güvenlik: ajanlar sadece kod işi araçlarını kullanır (dosya, kabuk, web, git); ekran/tarayıcı araçları kullanıcının
tek ekranını paylaştığı için kapalı. Yazma kapsamı verilen ajan kapsam dışına dosya araçlarıyla yazamaz; "readonly"
ajan hiç yazamaz. İzin soruları ajanın adıyla kullanıcıya gider.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult, make_diff, obj

LEAD = "alice"  # projenin yöneticisi
ORCHESTRATOR = LEAD  # eski ad
USER = "kullanıcı"  # Projeler sayfasından yazan kullanıcı
SYSTEM = "sistem"
COUNCIL = "kurul"
# Ajanların kullanabildiği araçlar: kod işi. Ekran, tarayıcı, masaüstü araçları kullanıcının tek ekranını paylaşır.
MEMBER_TOOLS = {"read_file", "write_file", "edit_file", "multi_edit", "apply_patch", "file_ops", "list_dir", "glob",
                "grep", "run_command", "process_output", "process_stop", "web_fetch", "web_search", "http_request",
                "todo_write", "git"}
WRITE_TOOLS = {"write_file", "edit_file", "multi_edit", "apply_patch", "file_ops"}
ISOLATIONS = ("shared", "worktree", "readonly")
THINKING = ("off", "low", "medium", "high", "default")
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
ACTIVE = ("running", "waiting", "asking")
FINAL = ("done", "failed", "stopped")
SAVE_EVERY = 1.5  # saniye: canlı güncellemelerde diske en sık bu aralıkla yazılır
DESIGN_FILE = "DESIGN.md"
RESEARCH_DIR = "research"  # referans sitelerin ekran görüntüleri ve çözümlemeleri (site_study yazar)


def research_refs(root: Path, limit: int = 2) -> dict[str, str]:
    """Araştırılmış referans sitelerin masaüstü görüntüleri (ad -> base64), karşılaştırma için."""
    import base64

    d = Path(root) / RESEARCH_DIR
    out: dict[str, str] = {}
    try:
        files = sorted(d.glob("*-masaustu.png"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return out
    for f in files[:limit]:
        try:
            out["referans-" + f.name[:-len("-masaustu.png")]] = base64.b64encode(f.read_bytes()).decode()
        except OSError:
            continue
    return out


def team_compact_cap(config) -> int:
    """Proje ajanlarının konuşması bu kadar token'ı geçince özetlenir ([agent] team_compact_at, 0 = sınırsız).
    Her adım bütün konuşmayı yeniden gönderir; 1M bağlamlı modelde sınır olmayınca bir proje ~100M token yakıyordu."""
    try:
        return max(0, int(config.agent.get("team_compact_at", 128000)))
    except (TypeError, ValueError):
        return 128000
MAX_MEMBERS = 8
ORIGINAL_LIMIT = 400_000  # geri alma/fark için saklanan dosya ilk hali (bayt); daha büyükleri saklanmaz


@dataclass
class Message:
    sender: str
    to: str
    text: str
    at: float = field(default_factory=time.time)

    def line(self) -> str:
        return f"[{self.sender} -> {self.to}] {self.text}"


@dataclass
class Member:
    name: str
    task: str
    constraints: str = ""
    scope: list[str] = field(default_factory=list)  # yazılabilecek yollar (boş: tüm çalışma klasörü)
    model: str = "auto"  # auto (işe göre akıllı seçim), rol adı (brain, fast...) ya da sağlayıcı:model
    thinking: str = "default"
    isolation: str = "shared"
    max_steps: int = 60
    after: list[str] = field(default_factory=list)  # bunlar onaylanınca başlar
    cwd: Path = Path(".")
    branch: str = ""
    base_sha: str = ""  # worktree açıldığı commit
    status: str = "queued"  # queued | running | waiting | asking | review | done | failed | stopped
    result: str = ""
    question: str = ""  # yöneticiye/kullanıcıya sorup cevap beklediği soru
    steps: int = 0
    reviews: int = 0  # kaç kez düzeltmeye gönderildi
    limit_hit: bool = False
    last: str = ""
    started: float = 0.0
    ended: float = 0.0
    files: list[str] = field(default_factory=list)  # dosya araçlarıyla değiştirdiği yollar (proje klasörüne göre)
    originals: dict[str, str | None] = field(default_factory=dict)  # mutlak yol -> ilk hali (None: yoktu)
    agent: Any = None
    thread: threading.Thread | None = None
    saved_messages: list[dict[str, Any]] = field(default_factory=list)  # diskten yüklenmiş konuşma (sistem hariç)
    resume_wanted: bool = False  # bitmişken iş geldi ama paralel sınırı dolu: yer açılınca devam edecek
    councils: list[dict[str, Any]] = field(default_factory=list)  # bu işin kurul sonuçları (eskiden yeniye)
    council_cycles: int = 0  # kurulun düzeltmeye gönderdiği tur sayısı
    council_running: bool = False
    last_at: float = 0.0  # son araç adımı (takılma tespiti)
    stall_warned: bool = False
    merge_attempts: int = 0
    provider_failed: str = ""  # bütün modeller düştüyse nedeni
    recovering: bool = False  # takıldığı için durduruldu; bitince başka modelle kaldığı yerden devam edecek
    recoveries: int = 0  # kaç kez otomatik kurtarıldı
    resume_note: str = ""  # bir sonraki devamda ajana söylenecek not
    picked: str = ""  # auto seçimde: "tasarım · glm-5.3"

    def snapshot(self) -> dict[str, Any]:
        now = self.ended or time.time()
        return {"name": self.name, "task": self.task, "model": self.model, "picked": self.picked, "thinking": self.thinking,
                "isolation": self.isolation, "status": self.status, "steps": self.steps, "last": self.last,
                "branch": self.branch, "scope": self.scope, "result": self.result[:1500], "files": self.files[-50:],
                "constraints": self.constraints, "after": self.after, "question": self.question,
                "reviews": self.reviews, "limit_hit": self.limit_hit, "council_running": self.council_running,
                "council": _council_brief(self.councils[-1]) if self.councils else None,
                "councils_n": len(self.councils), "secs": int(now - self.started) if self.started else 0}

    def messages(self) -> list[dict[str, Any]]:
        """Sistem istemi hariç konuşma: canlı ajandan ya da diskten."""
        if self.agent is not None:
            return self.agent.messages[1:]
        return self.saved_messages


def _add_usage(row: dict[str, float], usage: dict[str, Any]) -> None:
    """Token ve (sağlayıcı bildiriyorsa) ücret sayacı."""
    row["in"] = row.get("in", 0) + int(usage.get("prompt_tokens") or 0)
    row["out"] = row.get("out", 0) + int(usage.get("completion_tokens") or 0)
    cost = usage.get("cost")
    if isinstance(cost, (int, float)):
        row["cost"] = round(row.get("cost", 0.0) + float(cost), 6)


class _Stopped(Exception):
    """Ajan başlarken proje durduruldu."""


def human_failure(reasons: list[str]) -> str:
    """Router'ın ham hata satırlarını ("ref: neden") kısa, okunur bir açıklamaya çevirir."""
    out: list[str] = []
    for line in reasons:
        ref, _, why = line.partition(": ")
        name = ref.split(":", 1)[-1].split("/")[-1] or ref
        low = why.lower()
        if m := re.search(r"soğumada \((\d+) sn\)", why):
            secs = int(m.group(1))
            when = f"~{secs // 3600} sa" if secs >= 3600 else f"~{max(1, secs // 60)} dk"
            text = f"{name} beklemede ({when} sonra açılır)"
        elif "model sınırı" in low or "too_large" in low or "context" in low:
            text = f"{name} için konuşma çok uzun"
        elif any(w in low for w in ("quota", "kota", "429", "rate")):
            text = f"{name} kotası dolu"
        elif any(w in low for w in ("auth", "401", "403", "anahtar", " yok")):
            text = f"{name} için API anahtarı yok/geçersiz"
        elif any(w in low for w in ("timeout", "network", "unreachable", "bağlan", "veri göndermiyor")):
            text = f"{name} bağlantı sorunu"
        else:
            text = f"{name}: {why[:80]}"
        if text not in out:
            out.append(text)
    return ("; ".join(out[:4]) + (f" (+{len(out) - 4})" if len(out) > 4 else "")) or "hiçbir model cevap veremedi"


def _council_brief(c: dict[str, Any]) -> dict[str, Any]:
    res = c.get("result") or {}
    act = c.get("activity") or {}
    return {"id": c.get("id"), "kind": c.get("kind"), "state": c.get("state"), "decision": res.get("decision"),
            "score": res.get("score"), "round": c.get("round"), "rounds": c.get("rounds"), "phase": c.get("phase", ""),
            "done": sum(1 for a in act.values() if a.get("state") in ("done", "failed")),
            "total": len(c.get("roles") or act),
            "started": c.get("started")}


def _rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def in_scope(path: Path, scope: list[Path]) -> bool:
    return not scope or any(path == s or s in path.parents for s in scope)


def patch_paths(patch: str) -> list[str]:
    from alice.tools.patch import ADD, DELETE, MOVE, UPDATE

    out = []
    for line in (patch or "").splitlines():
        for prefix in (ADD, DELETE, UPDATE, MOVE):
            if line.startswith(prefix):
                out.append(line[len(prefix):].strip())
    return out


def write_targets(tool: str, args: dict[str, Any]) -> list[str]:
    """Dosya aracının yazacağı yollar (kapsam kontrolü ve fark için)."""
    if tool == "apply_patch":
        return patch_paths(str(args.get("patch") or ""))
    out = [str(args[k]) for k in ("path", "destination") if args.get(k)]
    out += [str(p) for p in args.get("paths") or []]
    return out


def _read_original(p: Path) -> str | None:
    if not p.exists() or p.is_dir():
        return None
    try:
        if p.stat().st_size > ORIGINAL_LIMIT:
            return "\0büyük"
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "\0okunamadı"


def _git(cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout)


def projects_dir() -> Path:
    from alice.config import alice_home

    d = alice_home() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slim(messages: list[dict[str, Any]], limit: int = 6000) -> list[dict[str, Any]]:
    """Diske yazılacak konuşma: görseller atılır, çok uzun araç çıktıları kısaltılır."""
    from alice.providers.router import strip_images

    out = []
    for m in strip_images(messages):
        m = dict(m)
        if isinstance(m.get("content"), str) and len(m["content"]) > limit:
            m["content"] = m["content"][:limit] + f"\n... [{len(m['content']) - limit} karakter kaydedilmedi]"
        out.append(m)
    return out


def list_projects() -> list[dict[str, Any]]:
    out = []
    for f in projects_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        members = data.get("members") or []
        status = data.get("status") or "stopped"
        out.append({"id": data.get("id") or f.stem, "title": data.get("title") or "", "goal": data.get("goal", ""),
                    "created": data.get("created", 0), "updated": data.get("updated", 0),
                    "origin": data.get("origin", "alice"), "cwd": data.get("cwd", ""),
                    "status": status if status in ("done", "stopped") else "stopped",
                    "questions": sum(1 for q in data.get("questions") or [] if not q.get("answer")),
                    "members": [{"name": m.get("name"), "status": m.get("status")} for m in members]})
    return sorted(out, key=lambda x: -float(x.get("updated") or x.get("created") or 0))


def guess_check(cwd: Path) -> str:
    """Projenin test komutunu tahmin et (yeni proje penceresinde öneri olarak)."""
    try:
        if (cwd / "package.json").is_file():
            data = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
            if (data.get("scripts") or {}).get("test"):
                return "npm test"
        if any((cwd / f).is_file() for f in ("pytest.ini", "tox.ini")) or (cwd / "tests").is_dir() or \
                "pytest" in ((cwd / "pyproject.toml").read_text(encoding="utf-8")
                             if (cwd / "pyproject.toml").is_file() else ""):
            return "python -m pytest -q"
        if (cwd / "Cargo.toml").is_file():
            return "cargo test"
        if (cwd / "go.mod").is_file():
            return "go test ./..."
    except (OSError, ValueError):
        pass
    return ""


def templates_dir() -> Path:
    from alice.config import alice_home

    d = alice_home() / "project_templates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tpl_file(name: str) -> Path:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower().replace("ı", "i").replace("ş", "s").replace("ğ", "g")
                  .replace("ü", "u").replace("ö", "o").replace("ç", "c")).strip("-")[:40]
    if not slug:
        raise ToolError("şablon adı boş")
    return templates_dir() / f"{slug}.json"


def list_templates() -> list[dict[str, Any]]:
    out = []
    for f in sorted(templates_dir().glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def delete_template(name: str) -> None:
    _tpl_file(name).unlink(missing_ok=True)



def _read_saved(f: Path, tries: int = 10) -> str:
    """Kayıt dosyasını oku. Windows'ta başka bir iş parçacığı dosyayı tam o an yenisiyle değiştirirken (replace)
    açmaya çalışmak PermissionError verir; kısa bekleyip tekrar denenir."""
    for i in range(tries):
        try:
            return f.read_text(encoding="utf-8")
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)
    return ""

class Team:
    """Bir proje: hedef, yönetici ve ajanlar."""

    def __init__(self, parent_ctx: ToolContext, goal: str, max_parallel: int = 5,
                 publish: Callable[[dict[str, Any]], None] | None = None, origin: str = "alice",
                 project_id: str = "", title: str = "", review: bool = True, lead_model: str = "auto",
                 lead_thinking: str = "default", cwd: Path | None = None, council: bool = True, check: str = ""):
        self.parent_ctx = parent_ctx
        self.goal = goal
        self.id = project_id or time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        self.title = title or (goal.strip().splitlines()[0][:70] if goal.strip() else "Proje")
        self.origin = origin  # alice: sohbetteki ALICE kurdu | user: kullanıcı Projeler sayfasından kurdu
        self.cwd = Path(cwd) if cwd else parent_ctx.cwd
        self.created = time.time()
        self.max_parallel = max(1, max_parallel)
        self.review = review  # biten işi yönetici incelesin mi
        self.council = council and review  # inceleme kurulu: plan, her iş ve final
        self.councils: list[dict[str, Any]] = []  # proje düzeyi kurullar: plan, final, kurula sor
        self.council_busy = 0
        self.final_runs = 0
        self.plan_rejects = 0
        self._final_pending = False
        self.check = (check or "").strip()  # kontrol komutu (testler): kurul öncesi otomatik çalışır
        self.lessons: list[str] = []  # kurulların bu projede yakaladığı sorunlar (sonraki işlere ders)
        self.timeline: list[dict[str, Any]] = []  # zaman çizelgesi: {at, who, kind, text}
        self.usage: dict[str, dict[str, float]] = {}  # kim -> {in, out, cost}: token ve ücret takibi
        self._live_councils: set[Any] = set()  # süren kurullar (proje durunca onlar da durur)
        self.picks: dict[str, str] = {}  # auto model seçimleri (ad -> "alan · model")
        self._screens_warned = False
        self.plan_council = True  # kurul açıkken plan da kurula gider (yeni proje penceresinden kapatılabilir)
        self.plan_prev_id = ""  # planı reddeden son kurulun kimliği (düzeltilmiş plan ona göre incelenir)
        self._lead_turns = 0
        self._plan_nudges = 0
        self._dirty0: set[str] | None = None  # proje başında zaten değişmiş dosyalar (komut takibi bunlara dokunmaz)
        self.lead_model = lead_model
        self.lead_thinking = lead_thinking if lead_thinking in THINKING else "default"
        self.members: dict[str, Member] = {}
        self.log: list[Message] = []
        self.inbox: dict[str, list[Message]] = {LEAD: []}
        self.questions: list[dict[str, Any]] = []  # yöneticinin kullanıcıya soruları
        self.summary = ""
        self.finished = False
        self.stopped = False
        self.lead_agent: Any = None
        self.lead_thread: threading.Thread | None = None
        self.lead_state = "idle"  # idle | running | done
        self.lead_last = ""
        self.lead_saved: list[dict[str, Any]] = []
        self._nudges = 0
        self.cond = threading.Condition()
        self.perm_lock = threading.Lock()  # izin pencereleri tek tek açılsın
        self.publish = publish or (lambda ev: None)
        self._live_ui = publish is not None  # uygulamada: bildirimler tıklanabilir proje kartı olarak gider
        self.roles: list[str] = []
        self._saved_at = 0.0
        self._save_lock = threading.RLock()
        self._pub_lock = threading.RLock()
        self._dir = projects_dir()  # kayıt klasörü proje kurulurken sabitlenir

    # ---------------------------------------------------------- zaman çizelgesi
    def mark(self, who: str, kind: str, text: str) -> None:
        with self.cond:
            self.timeline.append({"at": time.time(), "who": who, "kind": kind, "text": text[:300]})
            if len(self.timeline) > 600:
                del self.timeline[:100]

    # --------------------------------------------------------------- durum
    def status(self) -> str:
        if self.finished:
            return "done"
        if self.stopped:
            return "stopped"
        if any(not q.get("answer") for q in self.questions):
            return "needs_you"
        ms = list(self.members.values())
        if not ms:
            return "planning"
        if any(m.status in ACTIVE or m.status == "queued" for m in ms) or self.lead_state == "running":
            return "running"
        return "review"

    def active(self) -> bool:
        return not self.finished and not self.stopped

    def all_done(self) -> bool:
        """Ajanların hepsi son durumda (yönetici bitirmemiş olabilir)."""
        return bool(self.members) and all(m.status in FINAL and not m.resume_wanted for m in self.members.values())

    # ------------------------------------------------------------- mesajlar
    def send(self, sender: str, to: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise ToolError("mesaj boş")
        to = (to or "").strip().lower()
        if to == "all":
            targets = [n for n in self.members if n != sender] + ([LEAD] if sender != LEAD else [])
        else:
            targets = [to]
        unknown = [t for t in targets if t != LEAD and t not in self.members]
        if unknown:
            raise ToolError(f"projede böyle biri yok: {', '.join(unknown)}. Ajanlar: {', '.join(self.members) or '-'}, "
                            f"yönetici: {LEAD}")
        boss = sender in (LEAD, USER, COUNCIL)  # yönetici, kullanıcı ve kurul incelemedeki ajanı yeniden çalıştırır
        if sender == USER:
            self.stopped = False
            if LEAD in targets:
                self.finished = False  # bitmiş projeye kullanıcı yeni iş verdi
        resume = []
        with self.cond:
            msg = Message(sender, to, text)
            self.log.append(msg)
            for t in targets:
                self.inbox.setdefault(t, []).append(msg)
                m = self.members.get(t)
                if m is None:
                    continue
                if boss:
                    m.question = ""  # yönetici/kullanıcı cevap verdi
                if self.stopped:
                    continue
                if m.status in ("done", "failed") or (m.status == "review" and boss) or \
                        (m.status == "stopped" and sender == USER):
                    resume.append(m)
            if sender in self.members and to in (LEAD, USER):
                self.members[sender].question = text  # cevap beklenen soru
            self.cond.notify_all()
        for m in resume:  # bitmiş ajana iş geldi: konuşmasına kaldığı yerden devam etsin
            self._launch(m, resume=True)
        if LEAD in targets and sender != LEAD:
            self._ensure_lead()
        self._changed()
        return f"gönderildi: {', '.join(targets)}"

    def event(self, text: str) -> None:
        """Yöneticiye sistem olayı (ajan bitti, hata verdi...)."""
        with self.cond:
            self.inbox[LEAD].append(Message(SYSTEM, LEAD, text))
            self.cond.notify_all()
        self._ensure_lead()

    def take(self, name: str) -> list[Message]:
        with self.cond:
            msgs, self.inbox[name] = self.inbox.get(name, []), []
            return msgs

    def wait_messages(self, name: str, timeout: float, cancelled: Callable[[], bool] = lambda: False) -> list[Message]:
        end = time.monotonic() + timeout
        with self.cond:
            while not self.inbox.get(name) and not self.stopped and not cancelled():
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(min(left, 0.5))
            msgs, self.inbox[name] = self.inbox.get(name, []), []
            return msgs

    def wait_until(self, pred: Callable[[], bool], timeout: float, cancelled: Callable[[], bool] = lambda: False) -> bool:
        end = time.monotonic() + timeout
        with self.cond:
            while not pred():
                left = end - time.monotonic()
                if left <= 0 or cancelled():
                    return False
                self.cond.wait(min(left, 0.5))
            return True

    def ask_user(self, question: str) -> dict[str, Any]:
        q = {"id": secrets.token_hex(3), "text": question.strip(), "at": time.time(), "answer": ""}
        with self.cond:
            self.questions.append(q)
            self.log.append(Message(LEAD, USER, question.strip()))
            self.cond.notify_all()
        self._emit(f"'{self.title}' projesi sana soruyor: {question.strip()[:160]}", kind="ask")
        self._changed(save=True)
        return q

    def answer(self, qid: str, text: str) -> None:
        q = next((q for q in self.questions if q["id"] == qid), None)
        if q is None:
            raise ToolError("soru bulunamadı")
        if not (text or "").strip():
            raise ToolError("cevap boş")
        q["answer"] = text.strip()
        self.mark(USER, "answer", text.strip())
        self.send(USER, LEAD, f"Sorduğun soruya cevap ({q['text'][:200]}): {text.strip()}")

    # --------------------------------------------------------------- üyeler
    def add(self, spec: dict[str, Any]) -> Member:
        name = str(spec.get("name") or "").strip().lower()
        if not NAME_RX.match(name) or name in (LEAD, "all", USER, SYSTEM, COUNCIL):
            raise ToolError(f"geçersiz ajan adı '{name}': küçük harf/rakam/-/_, en çok 24 karakter; 'alice' ve 'all' "
                            "ayrılmış")
        if name in self.members:
            raise ToolError(f"'{name}' adı zaten var")
        if len(self.members) >= MAX_MEMBERS:
            raise ToolError(f"bir projede en çok {MAX_MEMBERS} ajan olur")
        task = str(spec.get("task") or "").strip()
        if not task:
            raise ToolError(f"{name}: task boş")
        iso = str(spec.get("isolation") or "shared")
        if iso not in ISOLATIONS:
            raise ToolError(f"{name}: isolation {', '.join(ISOLATIONS)} olmalı")
        think = str(spec.get("thinking") or "default")
        if think not in THINKING:
            raise ToolError(f"{name}: thinking {', '.join(THINKING)} olmalı")
        model = str(spec.get("model") or "auto").strip()
        self._check_model(model, name)
        after = [str(a).strip().lower() for a in spec.get("after") or [] if str(a).strip()]
        bad = [a for a in after if a not in self.members]
        if bad:
            raise ToolError(f"{name}: after içinde olmayan ajan: {', '.join(bad)} (önce onları ekle)")
        scope = [str(s) for s in spec.get("scope") or [] if str(s).strip()]
        m = Member(name=name, task=task, constraints=str(spec.get("constraints") or "").strip(), scope=scope,
                   model=model, thinking=think, isolation=iso, after=after,
                   max_steps=max(5, min(int(spec.get("max_steps") or 60), 200)))
        m.cwd = self._workdir(m)
        self.members[name] = m
        self.inbox[name] = []
        return m

    def _check_model(self, model: str, who: str) -> None:
        if model == "auto":
            return
        router = self.parent_ctx.router
        try:
            ok = router is None or bool(router.chain(model))
        except KeyError:
            ok = False
        if not ok:
            raise ToolError(f"{who}: '{model}' için model yok. auto, rol adı (brain, fast...) ya da sağlayıcı:model yaz.")

    def _repo_top(self) -> Path:
        r = _git(self.cwd, "rev-parse", "--show-toplevel", timeout=20)
        if r.returncode != 0:
            raise ToolError("worktree için çalışma klasörü bir git deposu olmalı")
        return Path(r.stdout.strip())

    def _workdir(self, m: Member) -> Path:
        base = self.cwd
        if m.isolation != "worktree":
            return base
        try:
            top = self._repo_top()
        except (OSError, subprocess.SubprocessError) as e:
            raise ToolError(f"{m.name}: worktree için çalışma klasörü bir git deposu olmalı ({e})") from e
        path = top / ".alice-team" / f"{self.id}-{m.name}"
        m.branch = f"alice-team/{m.name}-{time.strftime('%m%d-%H%M%S')}"
        m.base_sha = _git(top, "rev-parse", "HEAD").stdout.strip()
        r = _git(top, "worktree", "add", "-b", m.branch, str(path), "HEAD")
        if r.returncode != 0:
            raise ToolError(f"{m.name}: worktree açılamadı: {(r.stderr or r.stdout).strip()[:300]}")
        try:  # .alice-team git status'u kirletmesin
            excl = top / ".git" / "info" / "exclude"
            old = excl.read_text(encoding="utf-8", errors="replace") if excl.exists() else ""
            if excl.parent.is_dir() and ".alice-team/" not in old:
                with excl.open("a", encoding="utf-8") as f:
                    f.write("\n.alice-team/\n")
        except OSError:
            pass
        rel = base.resolve().relative_to(top.resolve()) if base.resolve() != top.resolve() else Path()
        return path / rel

    def start(self, plan: bool = False) -> None:
        """Projeyi başlat: ajanlar başlar, yönetici izlemeye (ya da ajan yoksa planlamaya) başlar."""
        self.mark(USER if self.origin == "user" else LEAD, "start", f"proje başladı: {self.title}")
        self._dirty0 = self._git_dirty()
        for m in self.members.values():
            self._launch(m)
        if plan or not self.members:
            self.event("Proje başladı ve henüz ajan yok. Kodu kısaca incele, işi çakışmayan parçalara böl ve planı "
                       "plan aracıyla sun" + (" (inceleme kurulu planı tartışıp oylayacak)" if self.council else "") +
                       ". İş küçükse tek ajan yeter.")
        else:
            self.event("Proje başladı; ajanlar çalışıyor. Şimdilik yapacak bir şey yoksa turunu bitir; soru ya da "
                       "inceleme gelince uyandırılırsın.")
        self._changed(save=True)

    def _running(self) -> int:
        return sum(1 for m in self.members.values() if m.status in ACTIVE)

    def _deps_ok(self, m: Member) -> bool:
        return all(self.members[a].status == "done" for a in m.after if a in self.members)

    def _launch(self, m: Member, resume: bool = False) -> None:
        with self.cond:
            if (self.stopped or self.finished) and not resume:
                return
            if m.status in ACTIVE:
                return
            if not resume and not self._deps_ok(m):
                m.status = "queued"
                return
            if self._running() >= self.max_parallel:
                if resume:
                    m.resume_wanted = True
                else:
                    m.status = "queued"
                return
            m.status = "running"
            m.resume_wanted = False
            m.started = m.started or time.time()
            m.ended = 0.0
        m.last_at, m.stall_warned = time.time(), False
        self.mark(m.name, "run", "devam ediyor" if resume else "başladı")
        m.thread = threading.Thread(target=self._run_member, args=(m, resume), daemon=True, name=f"team-{m.name}")
        m.thread.start()
        self._changed()

    def _next(self) -> None:
        for m in self.members.values():
            if self._running() >= self.max_parallel:
                return
            if m.status == "queued" and self._deps_ok(m):
                self._launch(m)
            elif m.resume_wanted and m.status not in ACTIVE:
                self._launch(m, resume=True)

    def _run_member(self, m: Member, resume: bool) -> None:
        final = "failed"
        try:
            if m.agent is None:
                m.agent = self._make_agent(m)
            if resume:
                msgs = self.take(m.name)
                prompt = ("[takım mesajları]\n" + "\n".join(x.line() for x in msgs) if msgs else "Devam et.") + \
                    "\n\nBunlara göre işine devam et; bitince yine kısa rapor yaz."
                if m.resume_note:
                    prompt, m.resume_note = m.resume_note + "\n\n" + prompt, ""
            else:
                prompt = "Görevine başla."
            m.limit_hit, m.provider_failed = False, ""
            if self.stopped:  # tam başlarken proje durduruldu
                raise _Stopped()
            text = m.agent.run_turn(prompt, max_steps=m.max_steps) or ""
            if self.stopped or m.agent.cancel.is_set():
                final = "stopped"
            elif m.provider_failed and not text.strip():
                final = "failed"  # hiçbir model cevap vermedi (kota/erişim): incelemeye değil hataya düşsün
                text = "Modellere ulaşılamadı: " + m.provider_failed
            else:
                final = "review" if self.review and m.isolation != "readonly" else "done"
            m.result = text.strip() or m.result or "(rapor yazmadı)"
        except _Stopped:
            final = "stopped"
        except Exception as e:  # noqa: BLE001 - bir ajanın çökmesi projeyi durdurmasın
            m.result = f"hata: {type(e).__name__}: {e}"
        finally:
            # Bitiş, kayıt diske yazıldıktan sonra görünsün: bekleyenler ve Projeler listesi tutarlı olsun.
            with self.cond:
                m.status, m.ended, m.question = final, time.time(), ""
                self.save()
                self.cond.notify_all()
        if final == "stopped" and m.recovering and not self.stopped:
            self._recover(m)
            return
        m.recovering = False  # kurtarma sürerken proje durdurulduysa bayrak kalmasın
        self.mark(m.name, final, {"review": "bitirdi, incelemede", "done": "bitirdi", "failed": "hata verdi",
                                  "stopped": "durdu"}.get(final, final) + (f": {m.result[:160]}" if final != "stopped" else ""))
        if final == "failed":
            self._emit(f"{self.title} / {m.name} hata verdi: {m.result[:200]}", kind="failed")
        if final == "review" and self.council:
            self._spawn(self._member_council, m)
        elif final == "review":
            self.event(self._review_note(m))
        elif final == "done":
            self.event(f"{m.name} bitirdi (inceleme yok). Raporu:\n{m.result[:3000]}")
            self._after_done(m)
        elif final == "failed":
            self.event(f"{m.name} hata verdi: {m.result[:600]}. Gerekirse message ile yeniden dene ya da işi "
                       "başka ajana ver.")
        self._next()
        self._changed()

    def slop_report(self, cwd: Path | None = None) -> str:
        """Tasarım projesinde sitedeki yapay zekâ kalıpları (koddan sayılır). Tasarım projesi değilse ya da temizse boş."""
        if not self.is_design():
            return ""
        from alice.design_check import lint, report

        try:
            return report(lint(cwd or self.cwd))
        except Exception:  # noqa: BLE001 - denetim projeyi durdurmasın
            return ""

    def _review_note(self, m: Member) -> str:
        files = ", ".join(m.files[-30:]) or "dosya değiştirmedi"
        if re.search(r"\.(html?|css|scss|jsx|tsx|vue|svelte)\b", files):
            slop = self.slop_report(m.cwd)
            if slop:
                files += ("\n\nOtomatik kalıp denetimi (sitede hâlâ yapay zekâ kalıpları var; bunlar ajanın alanındaysa "
                          "işi revise ile geri gönder):\n" + slop)
        extra = " Adım limiti doldu; iş yarım kalmış olabilir." if m.limit_hit else ""
        return (f"{m.name} bitirdi, inceleme bekliyor.{extra}\nRaporu:\n{m.result[:3000]}\n\nDeğişen dosyalar: {files}"
                + (f"\nDal: {m.branch} (onaylayınca birleştirilir)" if m.branch else "") +
                "\n\nİncele: diff ile farka bak, gerekirse testleri çalıştır; sonra review ile onayla ya da "
                "düzeltme iste.")

    def _after_done(self, m: Member) -> None:
        self._next()  # bu ajanı bekleyenler başlasın
        if not self.all_done() or self.finished:
            return
        if self.council and self.final_runs < 2:
            self._final_pending = True
            self._maybe_final()
            return
        self.event("Bütün ajanlar bitti. Bütünü doğrula (testler, derleme), gerekirse küçük entegrasyon "
                   "düzeltmesini yap, sonra finish ile kullanıcıya özet yaz.")

    # ----------------------------------------------------------------- kurul
    def _maybe_final(self) -> None:
        """Final kurulu, sürmekte olan kurullar (ör. son ajanın iş kurulu) bitince toplanır."""
        with self.cond:
            if not self._final_pending or self.council_busy or not self.all_done() or self.finished or self.stopped:
                return  # durmuş projede final kurulu toplanmaz; Sürdür'de yeniden denenir
            self._final_pending = False
            self.final_runs += 1
        self._spawn(self._final_council)

    def _spawn(self, fn: Callable[..., Any], *args: Any) -> None:
        with self.cond:
            self.council_busy += 1

        def run() -> None:
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001 - kurul hatası projeyi durdurmasın
                self.event(f"Kurul çalışamadı ({type(e).__name__}: {e}); incelemeyi kendin yap.")
            finally:
                with self.cond:
                    self.council_busy -= 1
                    self.cond.notify_all()
                self._maybe_final()
                self._changed(save=True)

        threading.Thread(target=run, daemon=True, name=f"council-{self.id}").start()

    def _council(self, kind: str, subject: str, context: str, store: list[dict[str, Any]], cwd: Path | None = None,
                 previous: dict[str, Any] | None = None, critical: bool = False) -> dict[str, Any]:
        from alice.council import Council

        slot = len(store)
        store.append({"kind": kind, "state": "running", "title": subject[:80], "result": {}, "votes": [],
                      "findings": [], "round": 0, "rounds": 0})
        if kind in ("work", "recheck", "final") and self.check:
            chk = self.run_check(cwd)
            if chk:
                context += (f"\n\n# Kontrol komutu: `{chk['cmd']}` → çıkış {chk['code']} "
                            f"({'GEÇTİ' if chk['code'] == 0 else 'BAŞARISIZ'})\n```\n{chk['out'][-3000:]}\n```")
        if kind in ("work", "recheck", "final", "adhoc"):
            slop = self.slop_report(cwd)
            if slop:
                context += ("\n\n# Otomatik kalıp denetimi: sitede yapay zekâ yapımı görünen kalıplar\nBunlar koddan "
                            "sayıldı, tahmin değil. Her biri 'major' tasarım sorunudur (iş hedefi 'AI yapımı gibi "
                            "durmasın'); küçük kod ayrıntılarından önce bunlara bak:\n" + slop)
        c = Council(self.parent_ctx, kind, subject, context, cwd=cwd or self.cwd, previous=previous,
                    critical=critical or self._critical(subject), rules=self.review_rules(),
                    lessons=self.lessons[-10:], check=self.check, images=self._screens(kind, subject + context, cwd))

        def live() -> None:
            store[slot] = c.snapshot()
            self._changed()

        c.on_change = live
        c.on_usage = lambda u: self._use(COUNCIL, u)
        self._live_councils.add(c)
        if self.stopped:
            c.stop()
        try:
            snap = c.run()
        finally:
            self._live_councils.discard(c)
        store[slot] = snap
        self.learn(snap)
        res = snap.get("result") or {}
        self.mark(COUNCIL, "council", f"{subject[:60]}: {res.get('decision', '?')} (%{round(100 * (res.get('score') or 0))})")
        self._changed(save=True)
        return snap

    def _screens(self, kind: str, text: str, cwd: Path | None) -> dict[str, str]:
        """Proje bir web sitesiyse ve iş görünüşe dokunuyorsa kurul için masaüstü ve telefon ekran görüntüleri."""
        if kind not in ("work", "recheck", "final", "adhoc"):
            return {}
        if kind != "final" and not re.search(r"\.(html?|css|scss|sass|less|jsx|tsx|vue|svelte|astro)\b", text):
            return {}
        if not bool(self.parent_ctx.config.agent.get("team_screenshots", True)):
            return {}
        from alice.site import find_entry, screenshots

        root = cwd or self.cwd
        entry = find_entry(root)
        if not entry:
            return {}
        shots = screenshots(root, entry)
        if shots and kind == "final":
            shots.update(research_refs(self.cwd))  # kurul bizim siteyi örnek alınan sitelerle yan yana görsün
        if not shots and not self._screens_warned:
            self._screens_warned = True
            note = ("Kurul siteyi göremedi (ekran görüntüsü alınamadı); sadece koda bakıyor. Görsel inceleme için "
                    "Edge ya da Chrome gerekir (ALICE_CHROMIUM ile tarayıcı yolu da verilebilir).")
            self.mark(SYSTEM, "council", note)
            self._emit(f"{self.title}: {note}", kind="info")
        return shots

    @staticmethod
    def _critical(text: str) -> bool:
        """İş kritik mi (kurul tüm uzmanlarla ve 3 turla toplanır)? Sadece görev metnine bakılır: fark/kod her yerde
        "token", "delete" içeriyor. Sözcük sınırı şart: "para" eskiden "paragraf"ı yakalayıp bir web sitesi projesinde
        16 kurulun hepsini kritik yapmıştı (kurul ~40M token)."""
        return bool(re.search(r"\b(?:güvenlik|security|şifre\w*|parola\w*|password|secret|kimlik doğrulama|auth\w*|"
                              r"veri kaybı|migration|göç\w*|ödeme\w*|payment|para(?:sı|yı|nın)?|kredi kartı|"
                              r"veritabanı silme|drop table)\b", text[:4000], re.I))

    def _member_material(self, m: Member) -> tuple[str, str]:
        subject = f"'{m.name}' ajanının işi. Görevi: {m.task}" + (f"\nSınırlar: {m.constraints}" if m.constraints else "")
        context = (f"Proje hedefi: {self.goal}\n\n# Ajanın raporu\n{m.result[:4000]}\n\n# Değişen dosyalar\n"
                   f"{', '.join(m.files) or '(yok)'}\n\n# Fark\n{self.diff(m.name, limit=20_000) or '(fark yok)'}")
        return subject, context

    def _member_council(self, m: Member, adhoc: bool = False) -> None:
        from alice.council import fix_list, report

        m.council_running = True
        self._changed()
        try:
            subject, context = self._member_material(m)
            done = [c for c in m.councils if c.get("state") != "stopped"]  # yarıda kesilen kurul önceki sayılmaz
            prev = done[-1] if done and m.council_cycles and not adhoc else None
            kind = "adhoc" if adhoc else ("recheck" if prev else "work")
            snap = self._council(kind, subject, context, m.councils, cwd=m.cwd, previous=prev)
        finally:
            m.council_running = False
        if snap.get("state") == "stopped":
            return  # proje durduruldu; sürdürülünce kurul yeniden toplanır
        with self.cond:
            self.log.append(Message(COUNCIL, m.name, report(snap).splitlines()[0]))
        self._apply_council(m, snap, report(snap), fix_list(snap))

    def _apply_council(self, m: Member, snap: dict[str, Any], rep_text: str, fixes: str) -> None:
        if m.status != "review":
            return  # bu arada kullanıcı/yönetici karar verdi
        decision = (snap.get("result") or {}).get("decision")
        from alice.council import blocking

        if decision == "approve_conditions" and not blocking(snap):
            # Şartlar sadece küçük ayrıntı: iş onaylanır, şartlar yöneticiye not olarak gider (yeni tur açılmaz).
            self.review_member(m.name, True, by=COUNCIL)
            self.event(f"Kurul {m.name} işini onayladı; küçük iyileştirme önerileri (zorunlu değil):\n{fixes}")
            return
        if decision == "approve":
            self.review_member(m.name, True, by=COUNCIL)
        elif decision in ("approve_conditions", "revise") and m.council_cycles < 2:
            m.council_cycles += 1
            self.review_member(m.name, False, "Kurul kararı: " + ("şartlı onay" if decision == "approve_conditions"
                                                                  else "düzeltme gerekli") + ". Şunları düzelt:\n"
                               + fixes, by=COUNCIL)
        elif decision in ("approve_conditions", "revise"):
            self.event(f"Kurul {m.name} işini iki düzeltme turundan sonra hâlâ kabul etmiyor. Kararı sen ver "
                       f"(review approve ya da revise; gerekirse ask_user).\n{rep_text}")
        else:
            self.event(f"Kurul {m.name} işinde ikiye bölündü. Raporu oku, gerekirse diff'e bak ve review ile karar ver "
                       f"(çok kritikse ask_user ile kullanıcıya sor).\n{rep_text}")

    def _final_council(self) -> None:
        from alice.council import report

        parts = []
        for m in self.members.values():
            parts.append(f"## {m.name} ({m.status})\nGörev: {m.task}\nRapor: {m.result[:1500]}\n"
                         f"Dosyalar: {', '.join(m.files) or '-'}\nFark:\n{self.diff(m.name, limit=6000) or '-'}")
        snap = self._council("final", f"Projenin tamamı: {self.goal}", "\n\n".join(parts)[:40_000], self.councils,
                             critical=True)
        if snap.get("state") == "stopped":  # sürdürülünce yeniden toplanır
            self.final_runs -= 1
            self._final_pending = True
            return
        decision = (snap.get("result") or {}).get("decision")
        last = self.final_runs >= 2
        if decision in ("approve", "approve_conditions"):
            note = "Final kurulu projeyi onayladı. Bütünü son kez doğrula ve finish ile özet yaz" + \
                (" (şartları özette açık kalan olarak belirt)." if decision == "approve_conditions" else ".")
        elif last:
            note = ("Final kurulu ikinci kez de onaylamadı. Kalan sorunları finish özetinde 'açık kalanlar' olarak yaz "
                    "ya da kritikse ask_user ile kullanıcıya sor.")
        else:
            note = ("Final kurulu onaylamadı. Bulguları ilgili ajanlara message ile görev olarak ver (ya da assign ile "
                    "yeni ajan kur); işler bitince final kurulu bir kez daha toplanır.")
        self.event(f"{report(snap)}\n\n{note}")

    def ask_council(self, name: str = "") -> None:
        """Kullanıcının 'Kurula sor' düğmesi: bir ajanın işini ya da (ad yoksa) projenin tamamını kurula sorar."""
        if self.council_busy and not name:
            raise ToolError("kurul zaten toplanıyor")
        if name:
            m = self.members.get(name)
            if m is None:
                raise ToolError(f"projede '{name}' yok")
            if m.status in ACTIVE or m.council_running:
                raise ToolError(f"{name} çalışıyor ya da kurulu sürüyor; bitince sor")
            self._spawn(self._member_council, m, True)
        else:
            def run() -> None:
                from alice.council import report

                parts = [f"## {m.name}: {m.task}\n{m.result[:1500]}\n{self.diff(m.name, limit=5000)}"
                         for m in self.members.values()]
                snap = self._council("adhoc", f"Projenin şu anki hali: {self.goal}", "\n\n".join(parts)[:40_000],
                                     self.councils)
                if snap.get("state") == "stopped":
                    return
                self.event(f"Kullanıcı kurula sordu, sonuç:\n{report(snap)}\nGerekiyorsa ajanlara yön ver.")

            self._spawn(run)

    def user_vote(self, name: str, vote: str, note: str = "") -> str:
        """Kullanıcının oyu (3 kat ağırlık): son kurul yeniden sayılır, karar değişirse uygulanır."""
        from alice.council import VOTES, fix_list, report, retally

        if vote not in VOTES:
            raise ToolError("oy approve, conditional ya da reject olmalı")
        store = self.members[name].councils if name in self.members else self.councils
        done = [i for i, c in enumerate(store) if c.get("state") in ("done", "failed")]
        if not done:
            raise ToolError("oylanacak kurul sonucu yok")
        i = done[-1]
        store[i] = retally(store[i], {"vote": vote, "note": note.strip()})
        snap = store[i]
        with self.cond:
            self.log.append(Message(USER, COUNCIL, f"oyum: {vote}" + (f" — {note.strip()}" if note.strip() else "")))
        if name in self.members:
            m = self.members[name]
            if m.status == "review":
                fixes = fix_list(snap) + (f"\n- kullanıcı: {note.strip()}" if note.strip() else "")
                dec = (snap.get("result") or {}).get("decision")
                if dec == "approve":
                    self.review_member(name, True, by=USER)
                elif dec in ("revise", "approve_conditions"):
                    self.review_member(name, False, "Kurul (senin oyunla) düzeltme istedi:\n" + fixes, by=USER)
        else:
            self.event(f"Kullanıcı kurulda oy verdi; yeni sonuç:\n{report(snap)}")
        self._changed(save=True)
        return (snap.get("result") or {}).get("decision") or "?"

    # -------------------------------------------------------------- inceleme
    def review_member(self, name: str, approve: bool, feedback: str = "", by: str = LEAD) -> str:
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        if approve:
            if m.status not in ("review", "done", "failed", "stopped"):
                raise ToolError(f"{name} şu an {m.status}; bitince onaylanabilir")
            if m.branch:
                try:
                    note = self._merge(m)
                except ToolError as e:
                    if m.merge_attempts < 2 and "birleştirilemedi" in str(e):
                        return self._conflict(m, str(e), by)
                    raise
            else:
                note = ""
            m.status = "done"
            self.mark(by, "approve", f"{name} onaylandı" + (f" ({note})" if note else ""))
            with self.cond:
                self.log.append(Message(by, name, "onaylandı" + (f": {feedback.strip()}" if feedback.strip() else "")))
                self.cond.notify_all()
            if by == USER:
                self.event(f"Kullanıcı {name} ajanının işini onayladı.")
            self._after_done(m)
            self._changed(save=True)
            return f"{name} onaylandı" + (f"; {note}" if note else "")
        if not feedback.strip():
            raise ToolError("düzeltme için feedback yaz: neyin yanlış olduğunu ve ne beklediğini söyle")
        m.reviews += 1
        self.mark(by, "revise", f"{name} düzeltmeye gönderildi: {feedback.strip()[:200]}")
        lessons = ("\n\nBu projede kurulun daha önce yakaladığı sorunlar (tekrarlama):\n" +
                   "\n".join(f"- {x}" for x in self.lessons[-8:])) if self.lessons else ""
        self.send(by, name, "İnceleme sonucu düzeltme gerekli:\n" + feedback.strip() + lessons)
        return f"{name} düzeltmeye gönderildi"

    def _conflict(self, m: Member, error: str, by: str) -> str:
        """Worktree dalı birleşmedi: ajan ana dalı kendi dalına alıp çakışmayı çözsün, sonra yeniden incelensin."""
        m.merge_attempts += 1
        base = _git(self._repo_top(), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
        self.mark(SYSTEM, "conflict", f"{m.name} dalı birleşmedi, ajan çakışmayı çözüyor")
        self.send(by if by in (LEAD, USER, COUNCIL) else LEAD, m.name,
                  f"İşin onaylandı ama dalın ana dala birleşmedi (çakışma). Kendi worktree'nde `git merge {base}` yap, "
                  f"çakışmaları çöz (iki tarafın amacını da koru), testleri çalıştır, commit et ve kısa rapor yaz; "
                  f"sonra yeniden incelenip birleştirilecek.\nHata: {error[:600]}")
        return f"{m.name} dalı birleşmedi; ajan çakışmayı çözüyor (deneme {m.merge_attempts}/2)"

    def _merge(self, m: Member) -> str:
        """Worktree ajanının dalını ana çalışma klasörüne birleştir (önce kalan değişiklikleri commit eder)."""
        top = self._repo_top()
        wt = m.cwd
        ident = ["-c", "user.name=ALICE", "-c", "user.email=alice@localhost"]
        if _git(wt, "status", "--porcelain").stdout.strip():
            _git(wt, "add", "-A")
            r = _git(wt, *ident, "commit", "-q", "-m", f"alice-team {m.name}: {m.task[:60]}")
            if r.returncode != 0:
                raise ToolError(f"{m.name} dalında commit yapılamadı: {(r.stderr or r.stdout).strip()[:300]}")
        if _git(top, "merge-base", "--is-ancestor", m.branch, "HEAD").returncode == 0:
            return "dalda birleştirilecek yeni bir şey yok"
        r = _git(top, *ident, "merge", "--no-ff", "--no-edit", m.branch)
        if r.returncode == 0:
            m.merge_attempts = 0
        if r.returncode != 0:
            _git(top, "merge", "--abort")
            raise ToolError(f"{m.branch} birleştirilemedi (çakışma ya da kirli çalışma klasörü): "
                            f"{(r.stdout + r.stderr).strip()[:500]}. Ajandan dalını güncellemesini iste ya da "
                            "kendin çöz.")
        return f"{m.branch} birleştirildi"

    def diff(self, name: str, limit: int = 60_000) -> str:
        """Ajanın dosya araçlarıyla yaptığı değişikliklerin farkı."""
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        parts = []
        for path, before in m.originals.items():
            p = Path(path)
            rel = _rel(p, m.cwd)
            after = _read_original(p)
            if (before or "").startswith("\0") or (after or "").startswith("\0"):
                parts.append(f"--- {rel}: dosya çok büyük ya da okunamadı, fark gösterilemiyor")
                continue
            if before == after:
                continue
            if after is None:
                parts.append(f"--- a/{rel}\n+++ /dev/null\n@@ dosya silindi @@")
                continue
            parts.append(make_diff(rel, before or "", after, max_lines=800))
        text = "\n".join(x for x in parts if x)
        return text[:limit] + ("\n... (kısaltıldı)" if len(text) > limit else "")

    def revert(self, name: str) -> list[str]:
        """Ajanın dosya araçlarıyla yaptığı değişiklikleri geri al."""
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        if m.status in ACTIVE:
            raise ToolError(f"{name} çalışıyor; önce durdur")
        done = []
        for path, before in m.originals.items():
            p = Path(path)
            if (before or "").startswith("\0"):
                continue
            try:
                if before is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(before, encoding="utf-8")
                done.append(_rel(p, self.cwd))
            except OSError:
                pass
        m.originals.clear()
        m.files.clear()
        with self.cond:
            self.log.append(Message(USER, name, f"değişiklikleri geri alındı ({len(done)} dosya)"))
        self._changed(save=True)
        return done

    def restart(self, name: str) -> None:
        """Ajanı temiz bir konuşmayla baştan başlat."""
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        if m.status in ACTIVE:
            raise ToolError(f"{name} çalışıyor; önce durdur")
        m.agent, m.saved_messages, m.result, m.steps, m.last = None, [], "", 0, ""
        m.started, m.ended, m.status = 0.0, 0.0, "queued"
        self.stopped = self.finished = False
        self._launch(m)
        self._changed(save=True)

    def finish(self, summary: str) -> None:
        self.mark(LEAD, "finish", "proje bitti")
        self.summary = summary.strip()
        self.lead_state = "done"
        with self._save_lock:  # "bitti" ancak kayıt yazılınca görünsün (liste ve yeniden açılış tutarlı)
            self.finished = True
            self.save()
        with self.cond:
            self.log.append(Message(LEAD, USER, "Proje bitti.\n" + self.summary))
            self.cond.notify_all()
        self._emit(f"'{self.title}' projesi bitti: {self.summary.splitlines()[0][:200] if self.summary else ''}",
                   kind="done")
        self._changed(save=True)

    # --------------------------------------------------------------- ajanlar
    def _role(self, name: str, model: str, thinking: str, text: str = "", domain: str = "") -> str:
        """Ajanın router rolü. model=auto: işin alanına göre (tasarım, kod, inceleme...) en uygun model sırası."""
        from alice.providers import picker

        role = f"team:{self.id}:{name}"
        router = self.parent_ctx.router
        if model == "auto":
            domain = domain or picker.classify(text)
            chain = picker.chain_for(router, domain, fallback="fast" if domain == "light" else "brain")
            self.picks[name] = picker.explain(chain[0], domain) if chain else ""
        else:
            chain = list(router.chain(model))
            self.picks[name] = ""
        router.role_overrides[role] = chain
        if thinking != "default":
            router.thinking_overrides[role] = thinking
        if role not in self.roles:
            self.roles.append(role)
        return role

    def _make_agent(self, m: Member):
        from alice.agent import Agent
        from alice.permissions import Permissions
        from alice.prompts import build_system_prompt

        parent = self.parent_ctx.state.get("agent")
        if parent is None or self.parent_ctx.router is None:
            raise RuntimeError("proje bu bağlamda çalışamıyor")
        ctx = ToolContext(cwd=m.cwd, config=self.parent_ctx.config, router=self.parent_ctx.router,
                          decider=self.parent_ctx.decider, emit=lambda s: None)
        tools = [t for n, t in parent.tools.items() if n in MEMBER_TOOLS] + [TeamSend(self, m.name),
                                                                            TeamWait(self, m.name)]
        if self.is_design() and m.isolation != "readonly":
            tools += [DesignCheck(self, m.name), SiteStudy(self, m.name)]
        perms = Permissions("plan" if m.isolation == "readonly" else parent.permissions.mode)
        perms.always_allow = set(parent.permissions.always_allow)
        agent = Agent(parent.config, parent.router, tools, ctx, perms, MemberUI(self, m), parent.decider,
                      system_prompt=build_system_prompt(m.cwd, personality=False) + "\n\n" + self.brief(m),
                      is_child=True)
        agent.stream = bool(parent.config.agent.get("stream", True))  # cevap yazılırken canlı aksın
        agent.compact_cap = team_compact_cap(parent.config)
        agent.role = self._role(m.name, m.model, m.thinking, text=f"{m.task}\n{m.constraints}")
        m.picked = self.picks.get(m.name, "")
        agent.on_usage = lambda u, _n=m.name: self._use(_n, u)
        agent.tool_specs = lambda _a=agent: [t.spec() for t in _a.tools.values()]  # gruplar yok: hepsi açık
        ctx.state["agent"] = agent  # uzun araçlar Durdur'u görsün
        scope = [ctx.resolve(s) for s in m.scope]
        base_run = agent._run_tool

        def guarded(tool: Tool, args: dict[str, Any]) -> ToolResult:
            targets = [ctx.resolve(p) for p in write_targets(tool.name, args)] if tool.name in WRITE_TOOLS else []
            if scope:
                bad = [str(p) for p in targets if not in_scope(p, scope)]
                if bad:
                    return ToolResult(f"kapsam dışı: {', '.join(bad)}. Sadece şuralara yazabilirsin: "
                                      f"{', '.join(m.scope)}. Başka bir yerin değişmesi gerekiyorsa sahibine ya da "
                                      f"{LEAD}'e team_send ile yaz. Kabuk komutuyla (Set-Content, Out-File, echo >) yazarak "
                                      "kapsamı aşma: dosya kodlamasını bozar (Türkçe harfler) ve iş reddedilir.", is_error=True)
            for p in targets:  # fark ve geri alma için ilk hali sakla
                m.originals.setdefault(str(p), _read_original(p))
            m.steps += 1
            m.last_at, m.stall_warned = time.time(), False
            m.last = f"{tool.name} {tool.summary(args)[:90]}"
            self._changed()
            res = base_run(tool, args)
            if tool.name == "run_command":
                self._track_command(m)
            if targets and not res.is_error:
                for p in targets:
                    rel = _rel(p, self.cwd)
                    if rel not in m.files:
                        m.files.append(rel)
            return res

        agent._run_tool = guarded

        def deliver() -> None:  # her araç adımından sonra gelen mesajları konuşmaya ekle
            msgs = self.take(m.name)
            if msgs:
                agent.messages.append({"role": "user", "content": "[takım mesajı]\n" +
                                       "\n".join(x.line() for x in msgs)})

        agent.on_step = deliver
        if m.saved_messages:  # diskten yüklenmiş proje: konuşmaya kaldığı yerden devam
            agent.messages = agent.messages[:1] + m.saved_messages
            m.saved_messages = []
        return agent

    def _git_dirty(self, cwd: Path | None = None) -> set[str] | None:
        """git deposundaysa değişmiş/yeni dosyalar (mutlak yol); değilse None."""
        cwd = cwd or self.cwd
        try:
            top = _git(cwd, "rev-parse", "--show-toplevel", timeout=10)
            if top.returncode != 0:
                return None
            r = _git(cwd, "status", "--porcelain", "-uall", timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        root = Path(top.stdout.strip())
        out = set()
        for line in r.stdout.splitlines():
            path = line[3:].split(" -> ")[-1].strip().strip('"')
            if path and not path.startswith(".alice-team/"):
                out.add(str((root / path).resolve()))
        return out

    def _track_command(self, m: Member) -> None:
        """run_command ile değişen dosyaları da ajanın değişikliklerine say (git deposunda): ilk hali HEAD'den alınır,
        proje başında zaten değişmiş ya da başka ajanın dokunduğu dosyalar atlanır."""
        base0 = self._dirty0 if m.isolation != "worktree" else set()
        if base0 is None:
            return
        now = self._git_dirty(m.cwd)
        if not now:
            return
        others = {k for o in self.members.values() if o is not m for k in o.originals}
        top = _git(m.cwd, "rev-parse", "--show-toplevel", timeout=10).stdout.strip()
        for path in sorted(now - set(base0) - set(m.originals) - others)[:50]:
            rel = Path(path).resolve().relative_to(Path(top).resolve()).as_posix() if top else path
            r = _git(m.cwd, "show", f"HEAD:{rel}", timeout=10)
            m.originals[path] = r.stdout if r.returncode == 0 else None
            relp = _rel(Path(path), self.cwd)
            if relp not in m.files:
                m.files.append(relp)

    def run_check(self, cwd: Path | None = None) -> dict[str, Any] | None:
        """Projenin kontrol komutu (testler/lint): kurul öncesi çalışır, çıktısı kurula kanıt olarak gider."""
        if not self.check:
            return None
        t0 = time.time()
        try:
            r = subprocess.run(self.check, shell=True, cwd=str(cwd or self.cwd), capture_output=True, text=True,
                               timeout=int(self.parent_ctx.config.agent.get("team_check_timeout", 600)),
                               encoding="utf-8", errors="replace")
            out, code = (r.stdout + r.stderr), r.returncode
        except subprocess.TimeoutExpired:
            out, code = "zaman aşımı", -1
        except OSError as e:
            out, code = str(e), -1
        res = {"cmd": self.check, "code": code, "out": out[-4000:], "secs": int(time.time() - t0)}
        self.mark(SYSTEM, "check", f"kontrol komutu {'geçti' if code == 0 else 'başarısız (çıkış ' + str(code) + ')'}")
        return res

    def review_rules(self) -> str:
        for f in (self.cwd / "REVIEW.md", self.cwd / ".alice" / "REVIEW.md"):
            try:
                if f.is_file():
                    return f.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                pass
        return ""

    def learn(self, snap: dict[str, Any]) -> None:
        """Kurulun doğruladığı ciddi bulgular projenin derslerine eklenir (sonraki ajanlara ve kurullara gider).
        Aynı dersi başka sözcüklerle tekrar eklemez."""
        from alice.council import dedupe

        new = [f"{f['title']}" + (f" → {f['fix']}" if f.get("fix") else "") for f in snap.get("findings", [])
               if f.get("status") == "confirmed" and f.get("severity") in ("critical", "major")]
        self.lessons += [x[:240] for x in dedupe(new, self.lessons)]
        del self.lessons[:-15]

    def brief(self, m: Member) -> str:
        roster = "\n".join(f"- {o.name}: {o.task[:160]}" + (f" (yazar: {', '.join(o.scope)})" if o.scope else "")
                           for o in self.members.values() if o.name != m.name) or "- (tek başınasın)"
        where = {"shared": "Diğerleriyle aynı klasörde çalışıyorsun; başkasının dosyasına dokunma.",
                 "worktree": f"Kendi git worktree'ndesin (dal {m.branch}); değişikliklerin bu dalda kalır, yönetici "
                             "onaylayınca birleştirilir.",
                 "readonly": "Salt okuma: dosya değiştiremez, değiştiren komut çalıştıramazsın; incele ve raporla."}
        lessons = ("Bu projede kurulun daha önce yakaladığı sorunlar (sen tekrarlama):\n" +
                   "\n".join(f"- {x}" for x in self.lessons[-8:]) + "\n") if self.lessons else ""
        after = f"Bu iş şunlar bittikten sonra başladı: {', '.join(m.after)}. Onların değişiklikleri hazır.\n" \
            if m.after else ""
        design = self.design_brief(m)
        return f"""# Proje görevi
Sen "{self.title}" projesinin "{m.name}" adlı ajanısın. Büyük bir işin bir parçasını yapıyorsun; kullanıcıyla değil
projenin yöneticisiyle (ALICE, adı "{LEAD}") ve takım arkadaşlarınla konuşursun.

Genel hedef: {self.goal}

Senin görevin: {m.task}
Sınırlar: {m.constraints or "özel bir sınır yok; yine de sadece kendi görevini yap."}
Yazma kapsamın: {", ".join(m.scope) if m.scope else "tüm çalışma klasörü (ama başkalarının alanına girme)"}
Çalışma klasörü: {m.cwd}. {where[m.isolation]}
{after}{lessons}{design}
Takım arkadaşların:
{roster}

Kurallar:
- Sadece kendi görevini yap. Başkasının alanındaki bir şeyin değişmesi gerekiyorsa ona team_send ile yaz.
- Başkalarını etkileyen bir arayüz/sözleşme (fonksiyon adı, API, veri biçimi) belirlediğinde erkenden ilgili kişiye
  ya da to="all" ile herkese haber ver.
- Karar gerekiyorsa, takıldıysan ya da kapsam dışına çıkman gerekiyorsa {LEAD}'e sor ve team_wait ile cevabı
  bekle. Gereksiz mesaj atma; kısa ve net yaz.
- Sana gelen mesajlar konuşmana "[takım mesajı]" olarak eklenir.
- Büyük dosyaları (yaklaşık 300 satırdan uzun) tek write_file ile yazma: önce iskeleti yaz, sonra bölüm bölüm
  edit_file ile doldur. Tek seferde dev bir araç çağrısı çok yavaş olur, kopabilir ve takılmış sanılırsın.
- İşini doğrula (test, çalıştırma). Bitince son mesajında kısa rapor yaz: ne yaptın, hangi dosyalar değişti,
  nasıl doğruladın, açık kalan ne. Yönetici işini inceler; düzeltme isterse yine sana yazar.
{self._member_design_rule(m)}"""

    def _member_design_rule(self, m: Member) -> str:
        if not self.is_design() or m.isolation == "readonly":
            return ""
        return ("- Tasarım kararı vermeden önce araştır: web_search ile konunun gerçek, iyi sitelerini bul, site_study "
                "ile bak (ekran görüntüsü + çözümleme). Kafandaki ortalama site değil, gördüğün iyi örnekler ölçüt.\n"
                "- Görünüşe dokunan işi bitirmeden önce design_check çalıştır: sitenin ekran görüntüsünü alır, yapay zekâ "
                "kalıplarını sayar ve bir sanat yönetmenine eleştirtir. Çıkan sorunları düzelt, sonra bir kez daha "
                "çalıştır. Kalıp listesi boş değilse işin reddedilir.\n")

    # ------------------------------------------------------------- yönetici
    def _ensure_lead(self) -> None:
        """Yönetici döngüsü çalışmıyorsa başlat (gelen kutusundaki olayları işler)."""
        with self.cond:
            if self.lead_thread is not None and self.lead_thread.is_alive():
                return  # döngü çalışıyor; olay gelen kutusundan alınacak
            if self.stopped:
                return  # durmuş projede yönetici çalışmaz; olaylar bekler, sürdürülünce işlenir
            if self.parent_ctx.state.get("agent") is None or self.parent_ctx.router is None:
                return
            self.lead_thread = threading.Thread(target=self._lead_loop, daemon=True, name=f"lead-{self.id}")
            self.lead_thread.start()

    def _make_lead(self):
        from alice.agent import Agent
        from alice.permissions import Permissions
        from alice.prompts import build_system_prompt

        parent = self.parent_ctx.state["agent"]
        ctx = ToolContext(cwd=self.cwd, config=self.parent_ctx.config, router=self.parent_ctx.router,
                          decider=self.parent_ctx.decider, emit=lambda s: None)
        tools = [t for n, t in parent.tools.items() if n in MEMBER_TOOLS] + [
            LeadPlan(self), LeadAssign(self), TeamSend(self, LEAD), LeadReview(self), LeadDiff(self), LeadAskUser(self),
            LeadFinish(self), LeadStatus(self)]
        perms = Permissions(parent.permissions.mode)
        perms.always_allow = set(parent.permissions.always_allow)
        agent = Agent(parent.config, parent.router, tools, ctx, perms, MemberUI(self, Member(name=LEAD, task="")),
                      parent.decider,
                      system_prompt=build_system_prompt(self.cwd, personality=False) + "\n\n" + self.lead_brief(),
                      is_child=True)
        agent.stream = bool(parent.config.agent.get("stream", True))  # cevap yazılırken canlı aksın
        agent.compact_cap = team_compact_cap(parent.config)
        agent.role = self._role(LEAD, self.lead_model, self.lead_thinking, domain="review")
        agent.on_usage = lambda u: self._use(LEAD, u)
        agent.tool_specs = lambda _a=agent: [t.spec() for t in _a.tools.values()]
        ctx.state["agent"] = agent
        if self.lead_saved:
            agent.messages = agent.messages[:1] + self.lead_saved
            self.lead_saved = []
        return agent

    def _lead_loop(self) -> None:
        try:
            if self.lead_agent is None:
                self.lead_agent = self._make_lead()
            while True:
                if self.stopped and not self.finished:
                    with self.cond:  # olaylar gelen kutusunda kalır; Sürdür ya da kullanıcının mesajı yeniden başlatır
                        if self.stopped:  # kilit altında yeniden bak: tam bu arada sürdürüldüyse çıkma
                            self.lead_thread = None
                    if self.lead_thread is None:
                        self.lead_state = "idle"
                        self._changed()
                        return
                msgs = self.wait_messages(LEAD, 1.0, lambda: self.finished or self.stopped)
                if msgs and self.stopped and not self.finished:
                    with self.cond:
                        self.inbox[LEAD] = msgs + self.inbox.get(LEAD, [])
                    continue
                if not msgs:
                    self._watch_stalls()
                    nudge = "" if self.finished or self.stopped else self._nudge()
                    if not nudge:
                        if self._lead_exit():
                            return
                        continue
                    msgs = [Message(SYSTEM, LEAD, nudge)]
                self.lead_state = "running"
                self._changed()
                prompt = "[olaylar]\n" + "\n\n".join(
                    (x.text if x.sender == SYSTEM else x.line()) for x in msgs) + "\n\n[durum]\n" + self.status_text()
                text = self.lead_agent.run_turn(prompt, max_steps=int(self.parent_ctx.config.agent.get(
                    "team_lead_steps", 40))) or ""
                self._lead_turns += 1
                self.lead_last = text.strip() or self.lead_last
                self.lead_state = "done" if self.finished else "idle"
                self._changed(save=True)
        except Exception as e:  # noqa: BLE001
            self.lead_state = "idle"
            self.lead_last = f"yönetici hata verdi: {type(e).__name__}: {e}"
            self._emit(self.lead_last)
            self._changed(save=True)

    def _lead_exit(self) -> bool:
        """Yöneticinin bekleyeceği bir şey kalmadıysa döngüden çık (yeni olay gelince _ensure_lead yeniden başlatır).
        Gelen kutusu kilit altında son kez kontrol edilir: çıkarken gelen olay kaybolmasın."""
        idle = all(m.status in FINAL + ("review",) and not m.resume_wanted for m in self.members.values()) and \
            not self.council_busy
        if not (self.stopped or self.finished or (self.members and idle and self._nudges >= 2)):
            return False
        with self.cond:
            if self.inbox.get(LEAD):
                return False
            self.lead_thread = None
            return True

    def _watch_stalls(self) -> None:
        """Uzun süredir ilerlemeyen ajan: önce yöneticiye ve kullanıcıya haber verilir; bir o kadar daha beklerse
        durdurulup sıradaki modelle kaldığı yerden sürdürülür (en fazla iki kez, sonra kullanıcıya bırakılır)."""
        limit = float(self.parent_ctx.config.agent.get("team_stall_minutes", 10)) * 60
        now = time.time()
        for m in list(self.members.values()):
            if m.status != "running" or not m.last_at or m.recovering:
                continue
            idle = now - m.last_at
            if not m.stall_warned and idle > limit:
                m.stall_warned = True
                mins = int(idle // 60)
                self.event(f"{m.name} {mins} dakikadır ilerlemiyor (son adım: {m.last or '-'}). Biraz daha "
                           "ilerlemezse sistem onu başka modelle kaldığı yerden yeniden başlatacak; sen de message ile "
                           "durumunu sorabilir ya da görevi küçültebilirsin.")
                self._emit(f"{self.title} / {m.name} {mins} dakikadır ilerlemiyor", kind="stall")
            elif m.stall_warned and idle > 2 * limit and m.agent is not None:
                if m.recoveries >= 2:
                    if m.recoveries == 2:  # bir kez haber ver
                        m.recoveries += 1
                        self.event(f"{m.name} iki otomatik yeniden başlatmadan sonra yine takıldı. Görevi böl, "
                                   "başka ajana ver ya da kullanıcıya sor.")
                        self._emit(f"{self.title} / {m.name} yine takıldı; bakman gerekebilir", kind="stall")
                    continue
                m.recovering = True
                self.mark(SYSTEM, "stall", f"{m.name} takıldı; durdurulup başka modelle sürdürülecek")
                m.agent.stop()

    def _recover(self, m: Member) -> None:
        """Takıldığı için durdurulan ajanı sıradaki modelle kaldığı yerden sürdür."""
        m.recovering = False
        m.recoveries += 1
        model = self._rotate_model(m)
        m.resume_note = ("[sistem] Önceki adımın uzun süre ilerlemediği için yarıda kesildi. Kaldığın yerden devam et. "
                         "Büyük dosyaları tek seferde yazma: önce iskeleti yaz, sonra bölüm bölüm düzenle.")
        self.mark(SYSTEM, "run", f"{m.name} otomatik yeniden başlatıldı" + (f" ({model})" if model else ""))
        self.event(f"{m.name} takılmıştı; sistem onu durdurup " + (f"{model} ile " if model else "") +
                   "kaldığı yerden sürdürdü. Bir şey yapmana gerek yok.")
        self._emit(f"{self.title} / {m.name} takıldı, otomatik yeniden başlatıldı", kind="info")
        self._launch(m, resume=True)
        self._changed(save=True)

    def _rotate_model(self, m: Member) -> str:
        """Ajanın model zincirinde bir sonrakine geç (takılan model sona gider). Yeni ilk modeli döndürür."""
        router = self.parent_ctx.router
        chain = router.role_overrides.get(f"team:{self.id}:{m.name}") if router is not None else None
        if not chain or len(chain) < 2:
            return ""
        chain.append(chain.pop(0))
        return str(chain[0])

    def _use(self, who: str, usage: dict[str, Any]) -> None:
        _add_usage(self.usage.setdefault(who, {}), usage)

    def usage_total(self) -> dict[str, float]:
        tot: dict[str, float] = {"in": 0, "out": 0}
        for row in self.usage.values():
            tot["in"] += row.get("in", 0)
            tot["out"] += row.get("out", 0)
            if "cost" in row:
                tot["cost"] = round(tot.get("cost", 0.0) + row["cost"], 6)
        return tot

    def _nudge(self) -> str:
        """Yönetici turunu bitirdi ama iş ortada kaldıysa hatırlat; iki kez olmazsa projeyi kendisi kapat."""
        if self.finished or self.stopped:
            return ""
        if not self.members:
            return self._plan_nudge()
        if any(not q.get("answer") for q in self.questions) or self._nudges >= 3 or self.council_busy or \
                any(m.council_running for m in self.members.values()):
            return ""
        pending = [m.name for m in self.members.values() if m.status == "review"]
        if pending and all(m.status in FINAL + ("review",) for m in self.members.values()):
            self._nudges += 1
            if self._nudges <= 2:
                return f"İnceleme bekleyen ajanlar var: {', '.join(pending)}. diff ile bak ve review ile karar ver."
            return ""
        if not self.all_done():
            return ""
        self._nudges += 1
        if self._nudges <= 2:
            return ("Bütün ajanlar bitti ama proje kapanmadı. Bütünü doğrula ve finish ile özet yaz "
                    "(yapacak iş kaldıysa assign ya da message ile devam et).")
        self.finish(self.lead_last or "Ajanlar işini bitirdi; yönetici özet yazmadı. Ajan raporlarına bak.")
        return ""

    def _plan_nudge(self) -> str:
        """Yönetici planlamadan turunu bitirdiyse hatırlat; iki kez olmazsa işi tek ajana ver (proje asılı kalmasın)."""
        if self._lead_turns == 0 or self.council_busy or self.lead_state == "running" or \
                any(not q.get("answer") for q in self.questions):
            return ""
        self._plan_nudges += 1
        if self._plan_nudges <= 2:
            return ("Henüz ajan kurmadın. Planı şimdi plan aracıyla sun (iş küçükse tek ajan yeter); açıklama yazıp "
                    "turu bitirme.")
        if self._plan_nudges == 3:
            self.mark(SYSTEM, "plan", "yönetici plan sunmadı; iş tek ajana verildi")
            self.add_member({"name": "worker", "task": f"Projenin hedefini baştan sona yap: {self.goal}\n"
                             "Önce kodu incele, sonra değişiklikleri yap, testlerle doğrula ve rapor yaz."},
                            announce=False)
        return ""

    def lead_brief(self) -> str:
        review = ("Biten her ajan 'review' durumuna geçer ve sana inceleme olayı gelir." if self.review else
                  "İnceleme kapalı: biten ajanlar doğrudan 'done' olur; yine de raporlarını oku.")
        council_plan = ""
        if self.council:
            review = ("Biten her işi inceleme kurulu (eleştirmen, savunucu, kontrolcü, gerekince uzmanlar) tartışıp "
                      "oylar ve kararı uygular; sana sadece çekişmeli ya da iki düzeltmeye rağmen kabul edilmeyen "
                      "işler gelir, onlarda kurul raporunu okuyup sen karar ver.")
            council_plan = ("; kurul planı inceler, planı bozan bir sorun bulursa bir kez düzeltip tekrar sunarsın, "
                            "kurulun önerileri ilgili ajanların görevine not olarak eklenir") if self.plan_council else ""
        return f"""# Proje yöneticisi
Sen ALICE'sin ve "{self.title}" projesinin yöneticisisin (orkestratör, ajanlar sana "{LEAD}" diye yazar).
Proje arka planda, kullanıcının sohbetinden bağımsız yürüyor. Kullanıcıyla sadece ask_user ve finish ile konuşursun.

Hedef: {self.goal}
Çalışma klasörü: {self.cwd}

Görevlerin:
1. Plan: ajan yoksa kodu kısaca incele (glob, grep, read_file) ve işi çakışmayan parçalara bölüp plan aracıyla
   bütün ajanları birden sun (en çok {MAX_MEMBERS}){council_plan}. Sonradan ek ajan için assign. Her ajana konuşmayı görmeyen birinin anlayacağı netlikte görev, doğrulama yolu ve yazma
   kapsamı (scope) ver; kapsamlar çakışmasın. model'i boş bırak (auto): sistem işin alanına göre (tasarım, kod,
   inceleme, metin, basit iş) o alanda en iyi modeli seçer; sadece özel bir model gerekiyorsa yaz. Zor
   tasarım/hata ayıklama: thinking=high; mekanik iş: thinking=low. Birbirine bağlı işlerde after kullan (ör. testler backend'den sonra). Aynı dosyalara
   dokunabilecek büyük değişikliklerde isolation=worktree. Sadece inceleme/araştırma için isolation=readonly.
2. İzle: sana olaylar "[olaylar]" mesajı olarak gelir. Ajan soru sorarsa message ile cevap ver. Kullanıcının tercihine
   bağlı, senin karar veremeyeceğin şeyleri ask_user ile sor (gereksiz soru sorma).
3. İncele: {review} Raporu oku, diff ile farka bak, gerekirse testleri çalıştır (run_command). Doğruysa
   review decision=approve; eksik ya da hatalıysa decision=revise ve somut geri bildirim (ne yanlış, ne bekliyorsun).
4. Bitir: herkes onaylanınca{" final kurulu toplanır;" if self.council else ""} bütünü doğrula (testler, derleme),
   gerekirse küçük bir entegrasyon düzeltmesini kendin yap, sonra finish ile kullanıcıya özet yaz: ne yapıldı, hangi
   dosyalar, nasıl doğrulandı, açık kalanlar.

{self._design_note()}Kurallar:
- Ajanların işini kendin yapma; sen planla, yönlendir, incele.
- Olay yoksa turunu bitir; bekleme aracı yok, yeni olay gelince uyandırılırsın.
- Kısa ve net yaz; ajanlara gereksiz mesaj atma.
"""

    def design_brief(self, m: Member) -> str:
        """Tasarım yönü belgesi varsa her ajana gösterilir (renk, yazı tipi, düzen kararları tek yerden)."""
        f = (m.cwd if m.cwd else self.cwd) / DESIGN_FILE
        if not f.is_file():
            f = self.cwd / DESIGN_FILE
        try:
            text = f.read_text(encoding="utf-8")[:5000] if f.is_file() else ""
        except OSError:
            text = ""
        return f"\n# Tasarım yönü ({DESIGN_FILE}) — buna uy, kendi renk/yazı tipi uydurma\n{text}\n" if text.strip() else ""

    def is_design(self) -> bool:
        """Proje görünüşü önemli bir iş mi (web sitesi, arayüz)?"""
        from alice.providers import picker

        return picker.classify(self.goal) == "design" or bool(re.search(r"web ?site|internet sitesi|arayüz|landing",
                                                                         self.goal.lower()))

    def _design_note(self) -> str:
        if not self.is_design():
            return ""
        return f"""Tasarım işi: bu projenin başarısı büyük ölçüde görünüşüne bağlı. Yapay zekâ yapımı gibi duran şablon
site başarısızlıktır; ne kadar çok sayfa ve kod olursa olsun. Modeller hep şu kalıplara kayıyor, hepsi YASAK:
- kod/terminal süsü etiketler ("AETH-SYS // BRİFİNG-01", "SEC-02 // ..."), ">" ile başlayan düğmeler ("> İNCELE"),
- sayaç/istatistik şeritleri ("128.400+", "%99,2", "640+"), sahte teknik notlar ("ŞEKİL 01", "ÇİZİM NO", "REV: D"),
- koyu lacivert/siyah zemin + tek neon vurgu rengi, neon parlama gölgeleri, gradyan yazı, buzlu cam kartlar,
- ızgara/tarama çizgisi/radar/parçacık arka planları, her yerde BÜYÜK HARF + mono yazı tipi,
- kutu içinde küçük çizim/diyagram, her bölümde aynı kart ızgarası, emoji,
- boş sloganlar ("Geleceği şekillendiriyoruz", "Otonom üstünlük", "yeni nesil", "tavizsiz").
Bu liste {DESIGN_FILE}'den üstündür: "teknik/askerî his" gibi bir yön bu kalıplarla verilmez.

Nasıl yapılır:
- Planın ilk ajanı "tasarim" olsun ve işe ARAŞTIRMAYLA başlasın (max_steps en az 70):
  1. web_search ile konunun gerçek, iyi tasarlanmış 4-6 sitesini bulsun (o sektörün öne çıkan firmaları; gerekirse
     tasarım ödülü almış benzer kurumsal siteler).
  2. Her birine site_study ile baksın: ekran görüntüsü + düzen, tipografi ölçüleri, renk oranları, görsel dil,
     metin tonu çözümlemesi. Açılmayanı atlayıp başkasını denesin; görmediği siteyi referans saymasın.
  3. {DESIGN_FILE}'yi bu araştırmaya dayandırsın. Sıfat değil ölçü ve karar: "başlık 72px/1.05, 600 ağırlık, en çok
     6 sözcük (X sitesindeki gibi)", "8px boşluk birimi, bölümler arası 160px", "%85 kırık beyaz, %12 grafit, %3
     vurgu", "ilk ekranın %70'i tam genişlik fotoğraf". Her önemli karar hangi referanstan alındığını söylesin.
     Ayrıca: kime ve hangi hisle konuşuyoruz, sayfa sayfa yerleşim taslağı, bileşen kuralları, hareket ilkeleri
     (az ve anlamlı), metin tonu (başlık örnekleriyle) ve yukarıdaki yasak listesini içeren bir "yapma" listesi.
  4. Sonra ortak stil temelini (değişkenler, temel bileşenler) kursun. Diğer ajanlar after ile onu beklesin.
- Görsel: soyut süs yerine gerçek fotoğraf en güçlü araçtır. Wikimedia Commons ya da Unsplash'ten konuya uygun
  fotoğraf kullanılabilir; adresi web_fetch ile doğrulanmalı, uydurma URL yazılmamalı. Fotoğraf yoksa güçlü
  tipografi ve boşluk; süs çizimi değil.
- Sayfa ajanlarının görevine "{DESIGN_FILE}'deki yöne uy, bitirmeden design_check çalıştır" de.
- İncelemede önce görünüşe bak: inceleme notunda "Otomatik kalıp denetimi" varsa iş o haliyle onaylanmaz. Şablon gibi
  durmak "önemli" bir sorundur; kurum adı, yazım, küçük kod ayrıntıları ondan sonra gelir.
- Az ama iyi: 6 vasat sayfa yerine 3 iyi sayfa daha değerlidir. Ajan sayısını işe göre tut.

"""

    # ----------------------------------------------------------------- durum
    def status_text(self) -> str:
        icon = {"queued": "…", "running": "▶", "waiting": "⏸", "asking": "?", "review": "◎", "done": "✓",
                "failed": "✗", "stopped": "■"}
        rows = []
        for m in self.members.values():
            s = m.snapshot()
            rows.append(f"{icon.get(m.status, '?')} {m.name} [{m.status}, {m.model}"
                        f"{', düşünme ' + m.thinking if m.thinking != 'default' else ''}, {m.steps} adım, {s['secs']} sn]"
                        + (f" sonra: {','.join(m.after)}" if m.after and m.status == "queued" else "")
                        + (f" dal={m.branch}" if m.branch else "") + (f" son: {m.last}" if m.last else ""))
        open_q = [q["text"] for q in self.questions if not q.get("answer")]
        if open_q:
            rows.append("Kullanıcıya açık sorular: " + " | ".join(open_q))
        return "\n".join(rows) or "(henüz ajan yok)"

    def snapshot(self) -> dict[str, Any]:
        done = sum(1 for m in self.members.values() if m.status == "done")
        return {"type": "team", "id": self.id, "title": self.title, "goal": self.goal, "origin": self.origin,
                "cwd": str(self.cwd), "created": self.created, "status": self.status(), "review": self.review,
                "summary": self.summary, "lead": {"state": self.lead_state, "model": self.lead_model,
                                                  "picked": self.picks.get(LEAD, ""),
                                                  "thinking": self.lead_thinking, "last": self.lead_last[:600]},
                "questions": self.questions[-20:], "progress": [done, len(self.members)], "council": self.council,
                "council_busy": self.council_busy, "councils": [_council_brief(c) for c in self.councils],
                "check": self.check, "lessons": self.lessons[-15:], "timeline_n": len(self.timeline),
                "usage": {"total": self.usage_total(), "by": self.usage},
                "members": [m.snapshot() for m in self.members.values()],
                "log": [{"from": x.sender, "to": x.to, "text": x.text[:400], "at": x.at} for x in self.log[-40:]],
                "active": self.active()}

    def _changed(self, save: bool = False) -> None:
        # Durumu alma ve yayınlama tek kilitte: iki iş parçacığı aynı anda yayınlarken eski durum yenisinin
        # ardından ekrana düşüp (ör. bitmiş projeyi "çalışıyor" gösterip) orada kalmasın.
        with self._pub_lock:
            try:
                self.publish(self.snapshot())
            except Exception:  # noqa: BLE001
                pass
        if save or time.monotonic() - self._saved_at > SAVE_EVERY:
            self.save()

    # ---------------------------------------------------------------- kayıt
    def path(self) -> Path:
        return self._dir / f"{self.id}.json"

    def save(self) -> None:
        # İçerik de kilit altında hazırlanır: yoksa eski içerik ("çalışıyor") yeni içeriğin ("bitti") üstüne
        # yazılabiliyor ve yeniden açılan proje yanlış durumda görünüyordu.
        try:
            with self._save_lock:  # ajanlar aynı anda kaydetmeye çalışabilir
                self._saved_at = time.monotonic()
                body = self._body()
                tmp = self.path().with_suffix(".json.tmp")
                tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(self.path())
        except (OSError, TypeError, ValueError, RuntimeError):
            pass

    def _body(self) -> dict[str, Any]:
        members = []
        for m in list(self.members.values()):
            d = m.snapshot()
            d.update(result=m.result, max_steps=m.max_steps, cwd=str(m.cwd), started=m.started, ended=m.ended,
                     base_sha=m.base_sha, messages=_slim(m.messages()), councils=m.councils[-6:],
                     council_cycles=m.council_cycles,
                     originals={k: v for k, v in list(m.originals.items()) if v is None or len(v) < 200_000})
            members.append(d)
        lead_msgs = self.lead_agent.messages[1:] if self.lead_agent is not None else self.lead_saved
        return {"id": self.id, "title": self.title, "goal": self.goal, "origin": self.origin, "cwd": str(self.cwd),
                "created": self.created, "updated": time.time(), "status": self.status(), "review": self.review,
                "lead_model": self.lead_model, "lead_thinking": self.lead_thinking, "summary": self.summary,
                "finished": self.finished, "lead_last": self.lead_last, "lead_messages": _slim(lead_msgs),
                "questions": self.questions, "members": members, "council": self.council,
                "councils": self.councils[-10:], "final_runs": self.final_runs, "plan_rejects": self.plan_rejects,
                "plan_council": self.plan_council, "plan_prev_id": self.plan_prev_id,
                "check": self.check, "lessons": self.lessons, "timeline": self.timeline[-500:], "usage": self.usage,
                "log": [{"from": x.sender, "to": x.to, "text": x.text, "at": x.at} for x in self.log[-500:]]}

    @classmethod
    def load(cls, project_id: str, parent_ctx: ToolContext, max_parallel: int = 5,
             publish: Callable[[dict[str, Any]], None] | None = None) -> "Team":
        f = projects_dir() / f"{Path(project_id).name}.json"
        data = json.loads(_read_saved(f))
        team = cls(parent_ctx, data.get("goal", ""), max_parallel, publish, origin=data.get("origin", "alice"),
                   project_id=data.get("id") or f.stem, title=data.get("title", ""),
                   review=bool(data.get("review", True)), lead_model=data.get("lead_model", "brain"),
                   lead_thinking=data.get("lead_thinking", "default"),
                   cwd=Path(data["cwd"]) if data.get("cwd") else None, council=bool(data.get("council", False)))
        team.councils = [c for c in data.get("councils") or [] if c.get("state") != "running"]
        team.check = str(data.get("check") or "")
        from alice.council import dedupe

        team.lessons = dedupe([str(x) for x in data.get("lessons") or []])
        team.timeline = list(data.get("timeline") or [])
        team.usage = {k: dict(v) for k, v in (data.get("usage") or {}).items() if isinstance(v, dict)}
        team.final_runs = int(data.get("final_runs") or 0)
        team.plan_rejects = int(data.get("plan_rejects") or 0)
        team.plan_council = data.get("plan_council") is not False
        team.plan_prev_id = str(data.get("plan_prev_id") or "")
        team.created = float(data.get("created") or time.time())
        team.summary = data.get("summary", "")
        team.finished = bool(data.get("finished"))
        team.lead_last = data.get("lead_last", "")
        team.lead_saved = list(data.get("lead_messages") or [])
        team.lead_state = "done" if team.finished else "idle"
        team.questions = list(data.get("questions") or [])
        for d in data.get("members") or []:
            m = Member(name=d["name"], task=d.get("task", ""), constraints=d.get("constraints", ""),
                       scope=list(d.get("scope") or []), model=d.get("model", "brain"),
                       thinking=d.get("thinking", "default"), isolation=d.get("isolation", "shared"),
                       max_steps=int(d.get("max_steps") or 60), after=list(d.get("after") or []),
                       cwd=Path(d.get("cwd") or team.cwd), branch=d.get("branch", ""), base_sha=d.get("base_sha", ""),
                       result=d.get("result", ""), steps=int(d.get("steps") or 0), reviews=int(d.get("reviews") or 0),
                       last=d.get("last", ""), started=float(d.get("started") or 0), ended=float(d.get("ended") or 0),
                       files=list(d.get("files") or []), originals=dict(d.get("originals") or {}),
                       saved_messages=list(d.get("messages") or []),
                       councils=[c for c in d.get("councils") or [] if c.get("state") != "running"],
                       council_cycles=int(d.get("council_cycles") or 0))
            # Kapanırken yarıda kalanlar durmuş sayılır; bitmiş, incelemedeki ve hatalılar olduğu gibi kalır.
            m.status = d.get("status") if d.get("status") in ("done", "failed", "review") else "stopped"
            m.ended = m.ended or team.created
            team.members[m.name] = m
            team.inbox[m.name] = []
        team.stopped = not team.finished  # yüklenen yarım proje kullanıcı sürdürene kadar durur
        team.log = [Message(x.get("from", "?"), x.get("to", "?"), x.get("text", ""), float(x.get("at") or 0))
                    for x in data.get("log") or []]
        return team

    def thread_view(self, name: str) -> dict[str, Any]:
        """Bir ajanın (ya da yöneticinin) konuşması ve bilgileri."""
        if name == LEAD:
            msgs = self.lead_agent.messages[1:] if self.lead_agent is not None else self.lead_saved
            return {"name": LEAD, "status": self.lead_state, "model": self.lead_model, "thinking": self.lead_thinking,
                    "task": "Projenin yöneticisi: planlar, soruları cevaplar, biten işleri inceler, sonucu özetler.",
                    "isolation": "shared", "steps": 0, "secs": 0, "files": [], "scope": [], "messages": msgs,
                    "result": self.summary or self.lead_last, "after": [], "constraints": "", "branch": "",
                    "question": "", "reviews": 0, "limit_hit": False, "last": "", "councils": self.councils}
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        return {**m.snapshot(), "messages": m.messages(), "councils": m.councils}

    member_view = thread_view

    def add_member(self, spec: dict[str, Any], announce: bool = True) -> Member:
        """Çalışan ya da bitmiş projeye yeni ajan ekle ve başlat; diğerlerine haber ver."""
        m = self.add(spec)
        self.stopped = self.finished = False
        self._nudges = 0
        if announce and len(self.members) > 1:
            others = [n for n in self.members if n != m.name]
            note = f"takıma yeni ajan katıldı: {m.name}: {m.task[:200]}" + (f" (yazar: {', '.join(m.scope)})" if m.scope else "")
            with self.cond:
                msg = Message(SYSTEM, "all", note)
                self.log.append(msg)
                for n in others:  # bitmiş ajanları uyandırmadan sadece haber ver
                    self.inbox[n].append(msg)
        self._launch(m)
        self._changed(save=True)
        return m

    def _emit(self, text: str, kind: str = "info") -> None:
        if kind in ("ask", "done", "stall", "failed"):
            self.mark(LEAD if kind != "failed" else SYSTEM, kind, text)
            self._notify(text, kind)
        try:
            if self._live_ui:
                self.publish({"type": "project_note", "id": self.id, "title": self.title, "text": text, "kind": kind})
            else:
                self.parent_ctx.emit(f"proje: {text}")
        except Exception:  # noqa: BLE001
            pass

    def _notify(self, text: str, kind: str) -> None:
        """Masaüstü bildirimi ve (ayarlıysa) telefona ntfy bildirimi; arka planda, hata projeyi etkilemez."""
        cfg = self.parent_ctx.config
        a = cfg.agent
        if not a.get("team_notify", True):
            return
        title = {"ask": "ALICE soruyor", "done": "Proje bitti", "stall": "Ajan takıldı",
                 "failed": "Ajan hata verdi"}.get(kind, "ALICE")

        def run() -> None:
            try:
                from alice.core.notify import Notification, desktop_notify

                desktop_notify(Notification(title, text[:300], "warn" if kind in ("ask", "stall", "failed") else "info",
                                            "project", kind="task"))
            except Exception:  # noqa: BLE001
                pass
            topic = str((cfg.data.get("notify") or {}).get("ntfy") or a.get("team_ntfy") or "").strip()
            if topic:
                url = topic if topic.startswith("http") else f"https://ntfy.sh/{topic}"
                try:
                    import httpx

                    # Başlık sorgu parametresiyle: HTTP başlıkları Türkçe karakter taşıyamaz.
                    httpx.post(url, content=text[:1000].encode("utf-8"), timeout=10,
                               params={"title": title, "tags": "robot"})
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=run, daemon=True, name="project-notify").start()

    def stop(self, name: str | None = None) -> None:
        targets = [self.members[name]] if name else list(self.members.values())
        self.mark(USER, "stop", f"{name} durduruldu" if name else "proje durduruldu")
        if not name:
            self.stopped = True
            self.lead_state = "idle"
            if self.lead_agent is not None:
                self.lead_agent.stop()
            for c in list(self._live_councils):
                c.stop()
        for m in targets:
            if m.status == "queued":
                m.status = "stopped"
            m.resume_wanted = False
            if m.agent is not None and m.status in ACTIVE:
                m.agent.stop()
        with self.cond:
            self.cond.notify_all()
        self._changed(save=True)

    def resume(self) -> None:
        """Durdurulmuş projeyi sürdür: durmuş ajanlar kaldığı yerden devam eder, yönetici uyanır."""
        self.mark(USER, "resume", "proje sürdürüldü")
        self.stopped = False
        self._nudges = 0
        for m in self.members.values():
            if m.status == "stopped":
                if m.agent is None and not m.saved_messages:
                    m.status = "queued"
                    self._launch(m)
                else:
                    self._launch(m, resume=True)
        for m in self.members.values():  # durdurulurken yarıda kalan iş kurulları yeniden toplanır
            if m.status == "review" and self.council and m.councils and m.councils[-1].get("state") == "stopped" \
                    and not m.council_running:
                self._spawn(self._member_council, m)
        self.event("Kullanıcı projeyi sürdürdü. Durumu gözden geçir, gerekiyorsa ajanlara yön ver.")
        self._maybe_final()
        self._changed(save=True)

    def save_template(self, name: str) -> dict[str, Any]:
        """Projeyi şablon olarak kaydet: hedef, ayarlar ve ajan tanımları (konuşmalar değil)."""
        tpl = {"name": name.strip()[:60], "goal": self.goal, "review": self.review, "council": self.council,
               "plan_council": self.plan_council,
               "lead_model": self.lead_model, "lead_thinking": self.lead_thinking, "check": self.check,
               "agents": [{"name": m.name, "task": m.task, "constraints": m.constraints, "scope": m.scope,
                           "model": m.model, "thinking": m.thinking, "isolation": m.isolation, "after": m.after,
                           "max_steps": m.max_steps} for m in self.members.values()]}
        _tpl_file(name).write_text(json.dumps(tpl, ensure_ascii=False, indent=1), encoding="utf-8")
        return tpl

    def timeline_view(self) -> list[dict[str, Any]]:
        """Zaman çizelgesi: olaylar ve mesajlar tek akışta, eskiden yeniye."""
        items = list(self.timeline) + [{"at": x.at, "who": x.sender, "kind": "msg", "text": f"→ {x.to}: {x.text[:300]}"}
                                       for x in self.log]
        return sorted(items, key=lambda x: x.get("at") or 0)[-400:]

    def cleanup(self) -> None:
        router = self.parent_ctx.router
        for role in self.roles:
            router.role_overrides.pop(role, None)
            router.thinking_overrides.pop(role, None)
        self.roles.clear()


class MemberUI:
    """Ajanın arayüzü: olayları proje durumuna yansıtır, izin sorularını ajanın adıyla kullanıcıya iletir. Model cevabı
    yazılırken metni ve düşünmesini canlı yayınlar (en sık 0,25 sn'de bir; kayda girmez)."""

    LIVE_EVERY = 0.25

    def __init__(self, team: Team, member: Member):
        self.team, self.member = team, member
        self.text, self.think, self.tool = "", "", ""
        self._last = 0.0

    def __getattr__(self, name: str):
        return lambda *a, **k: None

    def _live(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < self.LIVE_EVERY:
            return
        self._last = now
        try:
            self.team.publish({"type": "team_live", "id": self.team.id, "name": self.member.name,
                               "text": self.text[-8000:], "think": self.think[-4000:], "tool": self.tool})
        except Exception:  # noqa: BLE001
            pass

    def tool_delta(self, name: str, size: int) -> None:
        """Model bir aracı (ör. büyük bir dosya) yazarken: ilerleme sayılır, ekranda ne yazdığı görünür."""
        self.member.last_at, self.member.stall_warned = time.time(), False
        self.tool = f"{name} hazırlıyor · {size // 1000} KB" if size >= 1000 else f"{name} hazırlıyor"
        self._live()

    def stream_start(self) -> None:
        self.tool = ""
        self.text, self.think = "", ""
        self._live(force=True)
        self._last = 0.0  # ilk parça beklemeden görünsün

    def stream_delta(self, text: str) -> None:
        self.text += text
        self._live()

    def thinking_delta(self, text: str) -> None:
        self.think += text
        self._live()

    def stream_reset(self) -> None:
        self.text = ""
        self._live(force=True)

    def stream_end(self, text: str, source: str) -> None:
        self.text, self.think, self.tool = "", "", ""  # cevap artık konuşmada; canlı kutu kapanır
        self._live(force=True)

    def info(self, msg: str) -> None:
        if "adım limiti" in msg:
            self.member.limit_hit = True
        elif "hiçbir model cevap veremedi" in msg:
            reasons = [x.strip() for x in msg.splitlines()[1:] if x.strip()]  # ilk satır iç rol adını taşır
            self.member.provider_failed = human_failure(reasons)

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        if result.is_error:
            self.member.last = f"{name} hata: {result.text[:90]}"
            self.team._changed()

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        parent = self.team.parent_ctx.state.get("agent")
        ask = getattr(getattr(parent, "ui", None), "ask_permission", None)
        if ask is None:
            return "n"
        real = self.member.name in self.team.members
        prev = self.member.status
        if real:
            self.member.status = "waiting"
            self.team._changed()
        try:
            with self.team.perm_lock:
                ans = ask(name, f"[{self.team.title[:30]} / {self.member.name}] {summary}", risk)
        finally:
            if real and self.member.status == "waiting":
                self.member.status = prev
                self.team._changed()
        return "y" if ans == "a" else ans  # "her zaman" tek ajana değil, ana oturuma ait kalsın


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
    MAX_CRITIQUES = 4  # ajan başına; sonrasında sadece kalıp sayımı ve görüntü

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
    MAX_STUDIES = 8  # ajan başına

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
    """Kurulun ve yöneticinin göreceği kısa klasör özeti (iki seviye, gizli ve büyük klasörler hariç)."""
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
    """Plan kurulunun notlarını ilgili ajanın görevine 'kurul notu' olarak ekler; hiçbir ajana ait olmayanları döndürür."""
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
        specs = [dict(a) for a in agents or [] if isinstance(a, dict)]  # kopya: kurul notları yöneticinin geçmişine sızmasın
        if not specs:
            raise ToolError("en az bir ajan gerekli")
        if len(specs) + len(t.members) > MAX_MEMBERS:
            raise ToolError(f"bir projede en çok {MAX_MEMBERS} ajan olur")
        names = [str(a.get("name") or "").strip().lower() for a in specs]
        bad = [n for n in names if not NAME_RX.match(n) or n in (LEAD, "all", USER, SYSTEM, COUNCIL) or n in t.members]
        if bad or len(set(names)) != len(names):
            raise ToolError(f"geçersiz ya da tekrarlanan ajan adı: {', '.join(bad) or 'tekrar var'}")
        _check_specs(t, specs, names)  # kurul boşuna toplanmasın: tanım hataları önce
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
            # Düzeltilmiş plan, reddeden kurulun kendisine göre incelenir (kimliğiyle bulunur).
            prev = next((c for c in reversed(t.councils) if c.get("id") == t.plan_prev_id), None) \
                if t.plan_rejects and t.plan_prev_id else None
            t.council_busy += 1
            try:
                snap = t._council("plan", f"Proje planı: {t.goal}", context, t.councils, previous=prev)
            finally:
                with t.cond:
                    t.council_busy -= 1
            if snap.get("state") == "stopped":
                return ToolResult("Proje durduruldu; plan kurulu yarıda kaldı. Sürdürülünce planı tekrar sun.",
                                  is_error=True)
            failed = snap.get("state") == "failed"  # kurul çöktüyse plan reddedilmiş sayılmaz
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
        t.plan_rejects, t.plan_prev_id = 0, ""  # plan başlıyor: sonraki plan yeni plandır
        added = []
        for spec in specs:
            added.append(t.add_member(spec, announce=False).name)
        return ToolResult(f"Plan onaylandı, ajanlar başladı: {', '.join(added)}{cond_note}")


def _check_specs(t: Team, specs: list[dict[str, Any]], names: list[str]) -> None:
    """Ajan tanımlarını kurul toplanmadan önce doğrula (görev, çalışma yeri, düşünme, model, sıra)."""
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
