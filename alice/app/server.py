"""ALICE masaüstü uygulamasının yerel sunucusu.

Ajan bir thread'de çalışır, olaylar Server-Sent Events ile arayüze akar, arayüz
komutları POST ile gelir. Sadece 127.0.0.1'e bağlanır ve her istek oturuma özel
rastgele bir token ister: bu sunucu bilgisayarı tam kontrol eden bir ajana bağlı
olduğu için tarayıcıdaki başka bir sitenin ona istek atabilmesi kabul edilemez.
Token özel bir header'da gittiği için çapraz site istekleri CORS preflight'ta
takılır; Host kontrolü de DNS rebinding'i keser.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from alice import __version__
from alice import session as sess
from alice.agent import Agent, parse_args
from alice.config import Config, alice_home
from alice.decider import Decider
from alice.permissions import MODES, Permissions
from alice.providers.router import Router, parse_ref
from alice.tools import ToolContext, all_tools, attach_mcp, cleanup
from alice.tools.base import ToolResult

STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"app.js": "text/javascript", "projects.js": "text/javascript", "ui.js": "text/javascript",
                "main.js": "text/javascript", "app.css": "text/css", "marked.min.js": "text/javascript",
                "purify.min.js": "text/javascript", "highlight.js": "text/javascript", "icon.svg": "image/svg+xml", "alice.png": "image/png", "quick.js": "text/javascript",
                "quick.css": "text/css"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
       "frame-src 'self'")
# Önizlenen site korumalı çerçevede (ayrı köken) çalışır: ALICE'in sayfasına ve API'sine dokunamaz.
PREVIEW_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals"


# ----------------------------------------------------------------------- olaylar
class EventBus:
    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=5000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


class AppUI:
    """Agent'ın UI sözleşmesini olay yayınına çevirir. İzin soruları cevap gelene kadar bekler."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.pending: dict[str, dict[str, Any]] = {}
        self._answers: dict[str, str] = {}
        self._events: dict[str, threading.Event] = {}
        self.agent: Agent | None = None

    def stream_start(self) -> None:
        self.bus.publish({"type": "stream_start"})

    def stream_delta(self, text: str) -> None:
        self.bus.publish({"type": "delta", "text": text})

    def thinking_delta(self, text: str) -> None:
        self.bus.publish({"type": "thinking", "text": text})

    def stream_reset(self) -> None:
        self.bus.publish({"type": "stream_reset"})

    def stream_end(self, text: str, source: str) -> None:
        self.bus.publish({"type": "stream_end", "text": text, "source": source})

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self.bus.publish({"type": "tool_start", "id": call_id, "name": name, "summary": summary})

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        self.bus.publish({"type": "tool_end", "id": call_id, "name": name, "text": result.text[:20000],
                          "is_error": result.is_error, "diff": result.diff, "images": result.images[:4]})

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        pid = secrets.token_hex(6)
        ev = threading.Event()
        self._events[pid] = ev
        self.pending[pid] = {"type": "permission", "id": pid, "name": name, "summary": summary, "risk": risk}
        self.bus.publish(self.pending[pid])
        while not ev.wait(0.25):
            if self.agent is not None and self.agent.cancel.is_set():
                self._answers[pid] = "n"
                break
        self.pending.pop(pid, None)
        self._events.pop(pid, None)
        ans = self._answers.pop(pid, "n")
        self.bus.publish({"type": "permission_done", "id": pid, "answer": ans})
        return ans

    def answer(self, pid: str, ans: str) -> bool:
        ev = self._events.get(pid)
        if ev is None:
            return False
        self._answers[pid] = ans if ans in ("y", "n", "a") else "n"
        ev.set()
        return True

    def info(self, msg: str) -> None:
        self.bus.publish({"type": "info", "text": msg})


def _summary(tools: dict[str, Any], name: str, raw: str) -> str:
    tool = tools.get(name)
    if tool is not None:
        try:
            return tool.summary(parse_args(raw))
        except Exception:  # noqa: BLE001 - bozuk argüman: ham metni göster
            pass
    return raw if len(raw) < 160 else raw[:157] + "..."


def _user_text(m: dict[str, Any]) -> str:
    if m.get("_display"):
        return m["_display"]
    c = m.get("content")
    if isinstance(c, list):
        return next((p.get("text", "") for p in c if p.get("type") == "text"), "")
    return c or ""


def display_history(messages: list[dict[str, Any]], tools: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Mesaj geçmişini arayüzün çizebileceği öğelere çevir. Kullanıcı mesajları geri sarma için
    ajan geçmişindeki indekslerini (mi) taşır."""
    from alice.agent import PLAN_NOTE

    results = {m.get("tool_call_id"): m.get("content", "") for m in messages if m.get("role") == "tool"}
    items: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            if m.get("_image"):
                continue
            text = _user_text(m)
            if text.startswith("[Önceki konuşmanın özeti]"):
                items.append({"type": "info", "text": "önceki konuşma özetlendi"})
            elif not text.startswith("[eski ekran görüntüsü"):
                if text.startswith(PLAN_NOTE):
                    text = text[len(PLAN_NOTE):]
                items.append({"type": "user", "text": text, "mi": i, "n_images": m.get("_user_images", 0)})
        elif role == "assistant":
            if content and content != "Özeti aldım, kaldığım yerden devam ediyorum.":
                items.append({"type": "assistant", "text": content})
            for tc in m.get("tool_calls") or []:
                res = results.get(tc["id"], "")
                args = tc["function"].get("arguments", "")
                items.append({"type": "tool", "id": tc["id"], "name": tc["function"]["name"],
                              "summary": _summary(tools or {}, tc["function"]["name"], args),
                              "text": res, "is_error": res.startswith("HATA:")})
    return items


def git_branch(cwd: Path) -> str:
    """Çalışma klasörünün git dalı (durum çubuğu için). git çalıştırmadan .git/HEAD okunur."""
    for d in (cwd, *cwd.parents):
        g = d / ".git"
        try:
            if g.is_file():  # worktree / submodule: "gitdir: ..."
                line = g.read_text(encoding="utf-8").strip()
                if not line.startswith("gitdir:"):
                    return ""
                g = (d / line[7:].strip()).resolve()
            if g.is_dir():
                head = (g / "HEAD").read_text(encoding="utf-8").strip()
                return head[16:] if head.startswith("ref: refs/heads/") else head[:7]
        except OSError:
            return ""
    return ""


def _text_of(data: bytes | None) -> str | None:
    if data is None:
        return None
    if b"\0" in data[:4096]:
        return None
    for enc in ("utf-8", "cp1254"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def session_changes(checkpoints: dict[int, dict[str, bytes | None]], cwd: Path) -> list[dict[str, Any]]:
    """Bu oturumda dosya araçlarıyla değiştirilen dosyalar: ilk halleriyle şimdiki halleri arasındaki fark.
    Codex'teki inceleme paneli gibi tüm değişiklikler tek yerde görünsün."""
    import difflib

    original: dict[str, bytes | None] = {}
    for idx in sorted(checkpoints):
        for path, data in checkpoints[idx].items():
            original.setdefault(path, data)
    out: list[dict[str, Any]] = []
    for path, old in original.items():
        p = Path(path)
        try:
            new = p.read_bytes() if p.is_file() else None
        except OSError:
            continue
        if new == old:
            continue
        status = "eklendi" if old is None else "silindi" if new is None else "değişti"
        try:
            rel = str(p.relative_to(cwd))
        except ValueError:
            rel = str(p)
        rel = rel.replace("\\", "/")
        a, b = _text_of(old), _text_of(new)
        if (old is not None and a is None) or (new is not None and b is None):
            out.append({"path": rel, "abs": path, "status": status, "diff": "", "added": 0, "removed": 0, "binary": True})
            continue
        lines = list(difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(),
                                          f"a/{rel}", f"b/{rel}", lineterm="", n=3))
        added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
        out.append({"path": rel, "abs": path, "status": status, "diff": "\n".join(lines[:4000]),
                    "added": added, "removed": removed, "binary": False})
    out.sort(key=lambda c: c["path"])
    return out


def revert_change(checkpoints: dict[int, dict[str, bytes | None]], path: str) -> bool:
    """Tek bir dosyayı oturumdaki ilk haline döndür (Codex'teki dosya bazında "geri al")."""
    for idx in sorted(checkpoints):
        if path in checkpoints[idx]:
            data = checkpoints[idx][path]
            p = Path(path)
            if data is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            return True
    return False


# ------------------------------------------------------------------------ durum
class AppState:
    def __init__(self, cfg: Config, cwd: Path, mode: str, provider_factory=None):
        self.cfg = cfg
        self.bus = EventBus()
        self.ui = AppUI(self.bus)
        self.router = Router(cfg, provider_factory=provider_factory,
                             on_event=lambda s: self.bus.publish({"type": "route", "text": s}))
        self.decider = Decider.from_config(cfg, self.router)
        self.ctx = ToolContext(cwd=cwd, config=cfg, router=self.router, decider=self.decider,
                               emit=lambda s: self.bus.publish({"type": "info", "text": s}))
        self.agent = Agent(cfg, self.router, all_tools(cfg, self.ctx.emit), self.ctx, Permissions(mode), self.ui, self.decider)
        self.ui.agent = self.agent
        self.ctx.state["set_cwd"] = self.set_cwd  # github clone aracı çalışma klasörünü buradan değiştirir
        self.ctx.state["publish"] = self.bus.publish  # takım paneli canlı güncellensin
        from alice.memory import attach_memory

        attach_memory(self.agent, "uygulama")
        self.sid = sess.new_session_id()
        self.agent.on_step = lambda: sess.save(self.sid, self.agent.messages, self.ctx.cwd)
        self.token = secrets.token_urlsafe(24)
        # Önizleme adresindeki anahtar ayrı: sitenin kodu onu görse bile sadece proje dosyalarını okuyabilir.
        self.preview_token = secrets.token_urlsafe(18)
        self.allowed_hosts = {"127.0.0.1", "localhost"}  # telefon sunucusunda LAN adresi eklenir
        self.watch_store = None  # telefon sunucusu: saat verisi buraya yazılır
        self.quick_pending: dict[str, Any] | None = None  # hızlı bar açılırken doldurulacak metin/görsel
        self._stt = None
        # Tüm ajan işleri tek bir kalıcı thread'de çalışır: Playwright tarayıcısı ve UI Automation
        # nesneleri oluşturuldukları thread'e bağlı, her mesajda yeni thread açmak onları bozar.
        self._jobs: queue.Queue = queue.Queue()
        self._queued = False
        self._worker = threading.Thread(target=self._work, daemon=True, name="alice-agent")
        self._worker.start()
        # MCP sunucuları (npx ile açılanlar yavaş olabilir) arayüzü bekletmesin.
        self.run_in_worker(lambda: attach_mcp(self.agent, self.ctx,
                                              emit=lambda s: self.bus.publish({"type": "info", "text": s})))

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, done = job
            try:
                fn()
            except Exception:  # noqa: BLE001
                log_error()
            finally:
                if done is not None:
                    done.set()

    def run_in_worker(self, fn, wait: float | None = None) -> None:
        done = threading.Event()
        self._jobs.put((fn, done))
        if wait is not None:
            done.wait(wait)

    def shutdown(self) -> None:
        self.agent.stop()
        self.run_in_worker(lambda: cleanup(self.ctx), wait=10)
        self._jobs.put(None)

    def status(self) -> dict[str, Any]:
        a = self.agent
        return {
            "version": __version__,
            "model": self.router.last_ref.get("brain") or self.router.first_available("brain") or "",
            "mode": a.permissions.mode,
            "ctx_tokens": a.context_tokens(),
            "ctx_limit": a.compact_at,
            "ctx_window": self._ctx_window(),
            "today_requests": sum(r["requests"] for r in self.router.usage.today().values()),
            "decider": self.decider.names[0] if self.decider.names else "",
            "cwd": str(self.ctx.cwd),
            "branch": git_branch(self.ctx.cwd),
            "busy": a.busy or self._queued,
            "session": self.sid,
            "pending": list(self.ui.pending.values()),
            "brain_chain": self.router.chain("brain"),
            "brain_pinned": "brain" in self.router.role_overrides,
            "thinking": self.router.thinking_level("brain"),
            "user": _user_name(),
            "checkpoints": sorted(self.agent.checkpoints),
            "team": self.team_snapshot(),
        }

    def team_snapshot(self) -> dict[str, Any] | None:
        """Sohbetteki mini kart: ALICE'in son projesi; o bitmişse çalışan en yeni proje."""
        team = self.ctx.state.get("team")
        if team is None or not team.active():
            live = sorted((t for t in self._teams().values() if t.active()), key=lambda t: -t.created)
            team = live[0] if live else team
        return team.snapshot() if team is not None else None

    # -------------------------------------------------------------- projeler
    def _teams(self) -> dict[str, Any]:
        return self.ctx.state.setdefault("teams", {})

    def get_team(self, pid: str):
        """Canlı projeyi ya da diskteki projeyi (yükleyip) döndür."""
        from alice.team import Team
        from alice.tools.base import ToolError

        teams = self._teams()
        if pid in teams:
            return teams[pid]
        try:
            team = Team.load(pid, self.ctx, int(self.cfg.agent.get("team_max_parallel", 5)), self.bus.publish)
        except (OSError, ValueError, KeyError) as e:
            raise ToolError(f"proje bulunamadı: {pid}") from e
        teams[team.id] = team
        return team

    def projects(self) -> list[dict[str, Any]]:
        from alice.team import list_projects

        live = {t.id: t for t in self._teams().values()}
        rows = []
        for row in list_projects():
            t = live.pop(row["id"], None)
            rows.append(self._project_row(t) if t is not None else {**row, "active": False})
        rows = [self._project_row(t) for t in live.values()] + rows
        return rows

    @staticmethod
    def _project_row(t) -> dict[str, Any]:
        return {"id": t.id, "title": t.title, "goal": t.goal, "created": t.created, "updated": time.time(),
                "origin": t.origin, "cwd": str(t.cwd), "active": t.active(), "status": t.status(),
                "questions": sum(1 for q in t.questions if not q.get("answer")),
                "members": [{"name": m.name, "status": m.status} for m in t.members.values()]}

    def project_detail(self, pid: str, member: str = "") -> dict[str, Any]:
        team = self.get_team(pid)
        out = team.snapshot()
        out["log"] = [{"from": x.sender, "to": x.to, "text": x.text, "at": x.at} for x in team.log[-300:]]
        from alice.site import find_entry, last_change

        entry = find_entry(team.cwd)
        if entry:
            out["preview"] = {"entry": entry, "base": f"/preview/{self.preview_token}/{team.id}/"}
            if member == "__preview":
                out["preview"]["changed"] = last_change(team.cwd)
        if member == "__preview":
            return out  # site yoksa arayüz önizlemeyi kapatır
        if member == "__council":  # proje düzeyi kurullar: plan, final, kurula sor
            out["council_list"] = team.councils
            return out
        if member == "__timeline":
            out["timeline"] = team.timeline_view()
            return out
        if member:
            from alice.team import LEAD, LeadAssign, LeadDiff, LeadReview, TeamSend, TeamWait

            view = team.thread_view(member)
            agent = team.lead_agent if member == LEAD else team.members[member].agent
            tools = {**self.agent.tools, **({"team_send": TeamSend(team, member), "team_wait": TeamWait(team, member),
                                             "message": TeamSend(team, LEAD), "assign": LeadAssign(team),
                                             "review": LeadReview(team), "diff": LeadDiff(team)}
                                            if agent is None else agent.tools)}
            out["member"] = {**{k: v for k, v in view.items() if k != "messages"},
                             "history": display_history(view["messages"], tools)}
        return out

    def create_project(self, d: dict[str, Any]) -> str:
        from alice.team import THINKING, Team
        from alice.tools.base import ToolError

        goal = str(d.get("goal") or "").strip()
        specs = [a for a in d.get("agents") or [] if isinstance(a, dict)]
        if not goal:
            raise ToolError("hedef boş")
        if len(specs) > 8:
            raise ToolError("en çok 8 ajan")
        cwd = self.ctx.resolve(str(d["cwd"])) if d.get("cwd") else self.ctx.cwd
        if not cwd.is_dir():
            raise ToolError(f"klasör yok: {cwd}")
        lead_model = str(d.get("lead_model") or "auto").strip()
        lead_thinking = str(d.get("lead_thinking") or "default")
        team = Team(self.ctx, goal, int(self.cfg.agent.get("team_max_parallel", 5)), self.bus.publish, origin="user",
                    review=d.get("review") is not False, council=d.get("council") is not False, lead_model=lead_model,
                    check=str(d.get("check") or ""),
                    lead_thinking=lead_thinking if lead_thinking in THINKING else "default", cwd=cwd)
        team._check_model(lead_model, "yönetici")
        team.plan_council = d.get("plan_council") is not False
        for spec in specs:
            team.add(spec)
        self._teams()[team.id] = team
        team.start(plan=not specs)
        return team.id

    def project_action(self, pid: str, action: str, name: str = "", text: str = "",
                       agent: dict[str, Any] | None = None) -> str:
        from alice.team import USER
        from alice.tools.base import ToolError

        team = self.get_team(pid)
        if action == "stop":
            if name and name not in team.members:
                raise ToolError(f"projede '{name}' yok")
            team.stop(name or None)
            return "durduruldu"
        if action == "resume":
            team.resume()
            return "sürdürülüyor"
        if action == "send":
            return team.send(USER, name or "all", text)
        if action == "answer":
            team.answer(name, text)  # name: soru kimliği
            return "cevap iletildi"
        if action in ("approve", "revise"):
            return team.review_member(name, action == "approve", text, by=USER)
        if action == "save_template":
            tpl = team.save_template(text or team.title)
            return f"şablon kaydedildi: {tpl['name']}"
        if action == "vote":
            dec = team.user_vote(name, text, str((agent or {}).get("note") or ""))
            return f"oyun sayıldı; kurul kararı: {dec}"
        if action == "council":
            team.ask_council(name)
            return "kurul toplanıyor"
        if action == "restart":
            team.restart(name)
            return f"{name} baştan başladı"
        if action == "revert":
            files = team.revert(name)
            return f"{len(files)} dosya geri alındı"
        if action == "add":
            m = team.add_member(agent or {})
            return f"{m.name} eklendi"
        if action == "delete":
            if team.active():
                raise ToolError("önce projeyi durdur")
            team.cleanup()
            self._teams().pop(team.id, None)
            if self.ctx.state.get("team") is team:
                self.ctx.state.pop("team", None)
            team.path().unlink(missing_ok=True)
            return "silindi"
        raise ToolError(f"bilinmeyen işlem: {action}")

    def _ctx_window(self) -> int:
        """Şu anki ana modelin bağlam penceresi (arayüzde gösterilen sınır)."""
        try:
            ref = self.router.last_ref.get("brain") or self.router.first_available("brain")
        except KeyError:
            ref = None
        return self.router.context_limit(ref) if ref else 0

    def send(self, text: str, images: list[str] | None = None) -> tuple[bool, str | None]:
        if self.agent.busy or self._queued:
            return False, "ALICE şu an çalışıyor"
        prompt = text
        if text.startswith("/"):
            from alice.commands import INIT_PROMPT, council_prompt, expand_custom

            if text.split()[0] == "/init":
                prompt = INIT_PROMPT
            elif text.split()[0] == "/kurul":
                prompt = council_prompt(text[len("/kurul"):])
            else:
                prompt = expand_custom(text, self.ctx.cwd)
                if prompt is None:
                    return False, f"bilinmeyen komut: {text.split()[0]}"
        self._queued = True
        images = images or []

        def run() -> None:
            try:
                self.bus.publish({"type": "user", "text": text, "mi": len(self.agent.messages),
                                  "images": images[:6]})
                self.bus.publish({"type": "busy", "value": True})
                self.agent.run_turn(prompt, images=images or None)
                if prompt != text:  # özel komutlarda kullanıcı kendi yazdığını görsün
                    for m in reversed(self.agent.messages):
                        if m.get("role") == "user" and not m.get("_image"):
                            m["_display"] = text
                            break
            except Exception as e:  # noqa: BLE001 - thread içinde hata uygulamayı düşürmesin
                self.bus.publish({"type": "info", "text": f"beklenmeyen hata: {e}"})
                log_error()
            finally:
                try:
                    sess.save(self.sid, self.agent.messages, self.ctx.cwd)
                except OSError:
                    pass
                self._queued = False
                self.bus.publish({"type": "busy", "value": False})
                self.bus.publish({"type": "status", **self.status()})

        self.run_in_worker(run)
        return True, None

    def transcribe(self, data: bytes, filename: str) -> str:
        """Arayüzün mikrofon kaydını yazıya çevir (Whisper)."""
        if self._stt is None:
            from alice.voice.stt import make_stt

            self._stt = make_stt(self.cfg)
        return self._stt.transcribe_file(data, filename)

    def set_cwd(self, path: Path) -> None:
        """Çalışma klasörünü değiştir, sistem istemindeki klasörü güncelle ve sonraki açılış için hatırla."""
        from alice.prompts import build_system_prompt

        self.ctx.cwd = path
        if self.agent.messages and self.agent.messages[0].get("role") == "system":
            self.agent.messages[0]["content"] = build_system_prompt(path)
        try:
            alice_home().mkdir(parents=True, exist_ok=True)
            (alice_home() / "last_cwd").write_text(str(path), encoding="utf-8")
        except OSError:
            pass
        self.bus.publish({"type": "status", **self.status()})

    def github_repos(self, query: str = "") -> list[dict[str, Any]]:
        from alice.tools.github import GitHubAPI, clone_root, github_token

        root = clone_root(self.cfg)
        rows = GitHubAPI(github_token(self.cfg)).user_repos(query)
        for r in rows:
            local = root / r["full_name"]
            r["local"] = str(local) if (local / ".git").exists() else ""
        return rows

    def open_github(self, repo: str, branch: str = "") -> str:
        """Depoyu klonla (varsa güncelle) ve çalışma klasörü yap."""
        from alice.tools.github import clone_or_update

        path, note = clone_or_update(self.cfg, repo, branch)
        self.set_cwd(path)
        return note

    def rewind(self, mi: int) -> dict[str, Any]:
        text, restored = self.agent.rewind(mi)
        try:
            sess.save(self.sid, self.agent.messages, self.ctx.cwd)
        except OSError:
            pass
        return {"text": text, "restored": restored}

    def new_session(self) -> None:
        self.ctx.state.pop("team", None)  # takım proje olarak çalışmaya devam eder (Projeler sayfası)
        self.agent.reset()
        self.sid = sess.new_session_id()

    def resume(self, sid: str) -> bool:
        loaded = sess.load(sid)
        if not loaded:
            return False
        self.sid, msgs = loaded
        self.agent.messages = self.agent.messages[:1] + msgs
        return True


def _user_name() -> str:
    import getpass

    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""
    return name[:1].upper() + name[1:]


def log_error() -> None:
    try:
        with open(alice_home() / "app.log", "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{traceback.format_exc()}\n")
    except OSError:
        pass


# ----------------------------------------------------------------------- HTTP
def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ALICE"

        def log_message(self, *args: Any) -> None:  # sessiz
            pass

        # ------------------------------------------------------------ yardımcı
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in state.allowed_hosts

        def _token_ok(self, query: dict[str, list[str]]) -> bool:
            tok = self.headers.get("X-Alice-Token") or (query.get("t") or [""])[0]
            return secrets.compare_digest(tok, state.token)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 2_000_000:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {}

        def _deny(self) -> None:
            self._send(403, b"forbidden", "text/plain")

        def _preview(self, path: str) -> None:
            """/preview/<önizleme anahtarı>/<proje>/<yol>: proje klasöründeki siteyi korumalı olarak sun."""
            from urllib.parse import unquote

            from alice.site import content_type, safe_path
            from alice.tools.base import ToolError

            parts = path.split("/", 4)  # "", "preview", anahtar, proje, yol
            if len(parts) < 4 or not secrets.compare_digest(parts[2], state.preview_token):
                return self._deny()
            try:
                team = state.get_team(parts[3])
            except ToolError:
                return self._send(404, b"not found", "text/plain")
            f = safe_path(team.cwd, unquote(parts[4] if len(parts) > 4 else ""))
            if f is None:
                return self._send(404, b"not found", "text/plain")
            self._send(200, f.read_bytes(), content_type(f),
                       {"Content-Security-Policy": PREVIEW_CSP, "Access-Control-Allow-Origin": "*"})

        # --------------------------------------------------------------- GET
        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._host_ok():
                return self._deny()
            if u.path == "/":
                if not self._token_ok(q):
                    return self._deny()
                html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", state.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": CSP})
            if u.path == "/quick":
                if not self._token_ok(q):
                    return self._deny()
                html = (STATIC / "quick.html").read_text(encoding="utf-8").replace("{{TOKEN}}", state.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": CSP.replace("img-src 'self' data:",
                                                                          "img-src 'self' data:; media-src 'self' blob:")})
            if u.path.startswith("/preview/"):
                return self._preview(u.path)
            if u.path.startswith("/static/"):
                name = u.path[len("/static/"):]
                if name not in STATIC_FILES:
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, (STATIC / name).read_bytes(), STATIC_FILES[name])
            if not self._token_ok(q):
                return self._deny()
            if u.path == "/api/events":
                return self._events()
            if u.path == "/api/quick/pending":
                pending, state.quick_pending = state.quick_pending, None
                return self._json(pending or {})
            if u.path == "/api/state":
                return self._json(state.status())
            if u.path == "/api/history":
                return self._json(display_history(state.agent.messages, state.agent.tools))
            if u.path == "/api/sessions":
                rows = []
                for i, c, t in sess.list_sessions(60):
                    try:
                        updated = (sess.sessions_dir() / f"{i}.json").stat().st_mtime
                    except OSError:
                        updated = 0
                    rows.append({"id": i, "cwd": c, "title": t, "updated": updated})
                return self._json(rows)
            if u.path == "/api/changes":
                return self._json(session_changes(state.agent.checkpoints, state.ctx.cwd))
            if u.path == "/api/projects":
                return self._json(state.projects())
            if u.path == "/api/project/templates":
                from alice.team import list_templates

                return self._json(list_templates())
            if u.path == "/api/project/check_guess":
                from alice.team import guess_check

                raw = (q.get("cwd") or [""])[0]
                path = state.ctx.resolve(raw) if raw else state.ctx.cwd
                return self._json({"check": guess_check(path) if path.is_dir() else ""})
            if u.path == "/api/project/diff":
                from alice.tools.base import ToolError

                try:
                    team = state.get_team((q.get("id") or [""])[0])
                    return self._json({"diff": team.diff((q.get("member") or [""])[0])})
                except ToolError as e:
                    return self._json({"ok": False, "error": str(e)}, 404)
            if u.path == "/api/project":
                from alice.tools.base import ToolError

                try:
                    return self._json(state.project_detail((q.get("id") or [""])[0], (q.get("member") or [""])[0]))
                except ToolError as e:
                    return self._json({"ok": False, "error": str(e)}, 404)
            if u.path == "/api/files":
                from alice.mentions import complete_paths

                return self._json(complete_paths(state.ctx.cwd, (q.get("q") or [""])[0]))
            if u.path == "/api/commands":
                from alice.commands import catalog

                return self._json(catalog(state.ctx.cwd))
            if u.path == "/api/github/repos":
                from alice.tools.base import ToolError

                try:
                    return self._json({"ok": True, "repos": state.github_repos((q.get("q") or [""])[0])})
                except ToolError as e:
                    return self._json({"ok": False, "error": str(e)})
            if u.path == "/api/usage":
                return self._json(state.router.usage.today())
            if u.path == "/api/tools":
                return self._json([{"name": t.name, "risk": t.risk, "description": t.description.split("\n")[0][:160]}
                                   for t in state.agent.tools.values()])
            if u.path == "/api/memory":
                mem = state.agent.memory
                return self._json({"text": mem.export_markdown() if mem is not None else ""})
            if u.path == "/api/models/all":
                return self._json({"providers": state.router.catalog_view(),
                                   "brain": state.router.chain("brain"),
                                   "pinned": state.router.role_overrides.get("brain", [None])[0]})
            if u.path == "/api/models":
                return self._json({role: [{"ref": r, "status": st} for r, st in rows]
                                   for role, rows in state.router.status().items()})
            return self._send(404, b"not found", "text/plain")

        def _events(self) -> None:
            q = state.bus.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(f"data: {json.dumps({'type': 'status', **state.status()})}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                state.bus.unsubscribe(q)

        # -------------------------------------------------------------- POST
        def do_POST(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            if not self._host_ok() or not self._token_ok({}):
                return self._deny()
            d = self._body()
            p = u.path
            try:
                if p == "/api/send":
                    text = str(d.get("text", "")).strip()
                    images = [str(i) for i in (d.get("images") or [])][:6]
                    if not text and not images:
                        return self._json({"ok": False, "error": "boş mesaj"}, 400)
                    ok, err = state.send(text or "Bu görsele bak.", images)
                    return self._json({"ok": ok, "error": err}, 200 if ok else 409)
                if p == "/api/watch":
                    if state.watch_store is None:
                        return self._json({"ok": False, "error": "saat verisi bu sunucuda kapalı"}, 404)
                    from alice.health.watch import ingest

                    return self._json({"ok": True, "added": ingest(state.watch_store, d)})
                if p == "/api/transcribe":
                    import base64

                    from alice.voice.stt import STTError

                    try:
                        data = base64.b64decode(str(d.get("audio", "")))
                        ext = "ogg" if "ogg" in str(d.get("mime", "")) else "webm"
                        return self._json({"ok": True, "text": state.transcribe(data, f"speech.{ext}")})
                    except (STTError, ValueError) as e:
                        return self._json({"ok": False, "error": str(e)}, 502)
                if p == "/api/stop":
                    state.agent.stop()
                    return self._json({"ok": True})
                if p == "/api/permission":
                    return self._json({"ok": state.ui.answer(str(d.get("id")), str(d.get("answer")))})
                if p == "/api/open":
                    url = str(d.get("url", ""))
                    if url.startswith(("http://", "https://")):
                        import webbrowser

                        webbrowser.open(url)
                        return self._json({"ok": True})
                    return self._json({"ok": False}, 400)
                if p == "/api/project/template/delete":
                    from alice.team import delete_template
                    from alice.tools.base import ToolError

                    try:
                        delete_template(str(d.get("name", "")))
                    except ToolError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    return self._json({"ok": True})
                if p in ("/api/project/create", "/api/project/action"):
                    from alice.tools.base import ToolError

                    try:
                        if p == "/api/project/create":
                            pid = state.create_project(d)
                            return self._json({"ok": True, "id": pid})
                        pid = str(d.get("id", ""))
                        agent = d.get("agent") if isinstance(d.get("agent"), dict) else None
                        name = str(d.get("name", ""))
                        note = state.project_action(pid, str(d.get("action", "")),
                                                    name if d.get("action") == "answer" else name.lower(),
                                                    str(d.get("text", "")), agent)
                    except ToolError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    team = state._teams().get(pid)
                    return self._json({"ok": True, "note": note, "team": team.snapshot() if team else None})
                if p == "/api/mode":
                    mode = d.get("mode")
                    if mode not in MODES:
                        return self._json({"ok": False}, 400)
                    state.agent.permissions.mode = mode
                    return self._json({"ok": True, **state.status()})
                if (state.agent.busy or state._queued) and p in ("/api/new", "/api/resume", "/api/cwd", "/api/compact",
                                                                  "/api/rewind", "/api/changes/revert",
                                                                  "/api/github/open"):
                    return self._json({"ok": False, "error": "önce mevcut işi durdur"}, 409)
                if p == "/api/new":
                    state.new_session()
                    return self._json({"ok": True, **state.status()})
                if p == "/api/resume":
                    ok = state.resume(str(d.get("id", "")))
                    return self._json({"ok": ok, "history": display_history(state.agent.messages, state.agent.tools), **state.status()})
                if p == "/api/cwd":
                    path = state.ctx.resolve(str(d.get("path", "")))
                    if not path.is_dir():
                        return self._json({"ok": False, "error": f"klasör yok: {path}"}, 400)
                    state.set_cwd(path)
                    return self._json({"ok": True, **state.status()})
                if p == "/api/github/open":
                    from alice.tools.base import ToolError

                    try:
                        note = state.open_github(str(d.get("repo", "")), str(d.get("branch", "")))
                    except ToolError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    return self._json({"ok": True, "note": note, **state.status()})
                if p == "/api/rewind":
                    out = state.rewind(int(d.get("mi", -1)))
                    return self._json({"ok": True, **out, "history": display_history(state.agent.messages, state.agent.tools),
                                       **state.status()})
                if p == "/api/changes/revert":
                    ok = revert_change(state.agent.checkpoints, str(d.get("path", "")))
                    return self._json({"ok": ok, "changes": session_changes(state.agent.checkpoints, state.ctx.cwd)},
                                      200 if ok else 404)
                if p == "/api/session/delete":
                    sid = str(d.get("id", ""))
                    f = sess.sessions_dir() / f"{sid}.json"
                    if sid and f.parent == sess.sessions_dir() and f.exists():
                        f.unlink()
                    return self._json({"ok": True})
                if p == "/api/compact":
                    ok = state.agent.compact()
                    return self._json({"ok": ok, **state.status()})
                if p == "/api/thinking":
                    from alice.config import set_config_value
                    from alice.providers.thinking import LEVELS

                    level = str(d.get("level", ""))
                    if level not in LEVELS:
                        return self._json({"ok": False, "error": f"seviye: {' | '.join(LEVELS)}"}, 400)
                    state.router.thinking_overrides["brain"] = level
                    set_config_value("thinking", "brain", level)  # sonraki açılışta da kalsın
                    return self._json({"ok": True, **state.status()})
                if p == "/api/models/hide":
                    from alice.config import set_config_value

                    name, hide = str(d.get("name", "")).strip(), bool(d.get("hidden", True))
                    if not name or name.partition(":")[0] not in state.router.providers_cfg:
                        return self._json({"ok": False, "error": "bilinmeyen model ya da sağlayıcı"}, 400)
                    set_config_value("agent", "hidden_models", state.router.set_hidden(name, hide))
                    return self._json({"ok": True, **state.status()})
                if p == "/api/model":
                    role, ref = str(d.get("role", "")), str(d.get("ref", ""))
                    if ref == "reset":
                        state.router.role_overrides.pop(role, None)
                    else:
                        parse_ref(ref)
                        state.router.role_overrides[role] = [ref]
                    return self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                log_error()
                return self._json({"ok": False, "error": str(e)}, 500)
            return self._send(404, b"not found", "text/plain")

    return Handler


class AppServer:
    def __init__(self, state: AppState, port: int = 0, host: str = "127.0.0.1"):
        self.state = state
        self.httpd = ThreadingHTTPServer((host, port), make_handler(state))
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="alice-http")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/?t={self.state.token}"

    def start(self) -> "AppServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.state.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()

