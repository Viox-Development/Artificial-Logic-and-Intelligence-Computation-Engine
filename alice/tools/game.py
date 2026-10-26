"""Oyun oturumu aracı: bir oyunu turlar boyunca reflex'le oynamak ve oynarken öğrenmek (bkz. alice/reflex/game.py)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, READ, Tool, ToolContext, ToolError, ToolResult, obj

STATE_KEY = "game_session"


class Game(Tool):
    name = "game"
    description = (
        "Herhangi bir oyunu turlar boyunca oynar ve oynarken öğrenir (sıra tabanlı strateji, kart, bulmaca...). Tek "
        "reflex çağrısından farkı: her turdan sonra ekranda gerçekten ne olduğuna bakıp ders çıkarır (işe yaramayan "
        "tıklama, gidip gelme, hangi düğme ne açıyor), dersleri oturumlar arası saklar ve sonraki adımlarda ekrana "
        "uyanları kullanır; ekranın sadece değişen kısımlarını okur; tur özetleri tutar.\n"
        "- start: oyunu ve penceresini seç (oyun önceden app/windows ile açık olmalı). Ders yoksa ilk ekrandan "
        "başlangıç dersleri çıkarır.\n"
        "- turn: goal'u ekranda yap (ör. 'end the turn', 'pick a research'). Büyük hedefi küçük turlara böl.\n"
        "- note: kalıcı bir not ekle (kullanıcının söylediği ya da emin olduğun bir şey, kısa, İngilizce).\n"
        "- status: dersler, notlar ve son tur özetleri. end: oturumu kapat.\n"
        "Giriş, şartları kabul, gizlilik onayı, satın alma ya da reklam ekranı çıkarsa başka araçla da geçme: dur, "
        "kullanıcıya bırak."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["start", "turn", "note", "status", "end"],
                   "description": "start: oturumu aç | turn: bir hedefi oyna | note: not ekle | status | end."},
        "game": {"type": "string", "description": "start için oyunun adı."},
        "title": {"type": "string", "description": "start için pencere başlığından parça. Boş: oyunun adı."},
        "goal": {"type": "string", "description": "turn için bu turda ekranda yapılacak kısa ve somut hedef."},
        "note": {"type": "string", "description": "note için eklenecek bilgi."},
        "max_steps": {"type": "integer", "description": "turn için en fazla adım. Varsayılan 15."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return COMPUTER if args.get("action") in ("turn", "start") else READ

    def run(self, ctx: ToolContext, action: str, game: str = "", title: str = "", goal: str = "", note: str = "",
            max_steps: int = 15) -> ToolResult:
        from alice.config import alice_home
        from alice.reflex.game import GameSession

        sess: GameSession | None = ctx.state.get(STATE_KEY)
        if action == "start":
            if not game.strip():
                raise ToolError("start için game (oyunun adı) gerekli")
            sess = GameSession.open(alice_home(), game, title)
            ctx.state[STATE_KEY] = sess
            found = self._discover(ctx, sess) if not sess.lessons() and not sess.notes() else ""
            return ToolResult(f"oyun oturumu: {sess.name} (pencere: '{sess.title}', şimdiye kadar {sess.turn} tur)\n"
                              f"dersler: {sess.lessons_path}\n{found}\n{sess.context()}")
        if sess is None:
            raise ToolError("açık oyun oturumu yok: önce action=start, game=<oyunun adı>")
        if action == "note":
            added = sess.add_note(note)
            return ToolResult("not eklendi" if added else "not boş ya da zaten var")
        if action == "status":
            return ToolResult(f"{sess.name}: {sess.turn} tur, {len(sess.lessons())} ders\n\n{sess.context()}")
        if action == "end":
            ctx.state.pop(STATE_KEY, None)
            return ToolResult(f"oyun oturumu kapandı: {sess.name}, toplam {sess.turn} tur, {len(sess.lessons())} ders; "
                              f"hepsi {sess.root} altında")
        if action != "turn":
            raise ToolError(f"bilinmeyen action: {action}")
        if not goal.strip():
            raise ToolError("turn için goal gerekli")
        return self._turn(ctx, sess, goal, max_steps)

    @staticmethod
    def _chat(ctx: ToolContext):
        if ctx.router is None:
            return None

        def chat(messages: list[dict[str, str]]) -> str:
            return ctx.router.chat("fast", messages, temperature=0, max_tokens=700).text

        return chat

    def _observer(self, ctx: ToolContext, sess: Any, goal: str) -> Any:
        from alice.reflex.observers import DesktopObserver

        if sess.observer is None:
            sess.observer = DesktopObserver(ctx, sess.title, goal, diff=True)
        return sess.observer

    def _discover(self, ctx: ToolContext, sess: Any) -> str:
        """Hiç bilinmeyen oyun: ilk ekran + modelin bildikleri -> doğrulanmamış başlangıç dersleri."""
        from alice.reflex.game import discover

        try:
            visible = [i.label for i in self._observer(ctx, sess, "").screen("").items]
        except Exception as e:  # noqa: BLE001 - ekran okunamazsa keşifsiz başla
            return f"ilk ekran okunamadı ({e}); dersler oynadıkça birikecek\n"
        n = sess.apply(discover(self._chat(ctx), sess.name, visible), source="prior", seen_texts=visible)["add"]
        return f"ilk ekrandan {n} başlangıç dersi çıkarıldı (doğrulanmamış)\n"

    def _turn(self, ctx: ToolContext, sess: Any, goal: str, max_steps: int) -> ToolResult:
        from alice.config import alice_home
        from alice.reflex.agent import ScreenAgent, make_translator
        from alice.reflex.game import code_lessons, reflect
        from alice.reflex.loop import make_text_writer
        from alice.tools.meta import reflex_verdict

        if ctx.decider is None or ctx.router is None:
            raise ToolError("karar motoru yapılandırılmamış")
        agent = ScreenAgent(self._observer(ctx, sess, goal), ctx.decider, make_text_writer(ctx.router),
                            make_translator(ctx.router), emit=ctx.emit, trace_path=alice_home() / "reflex-trace.jsonl")
        agent.track_changes = True
        res = agent.run(goal, max_steps=max_steps, notes=lambda scr: sess.context([i.label for i in scr.items]))
        visible = [i.label for i in (agent.last.items if agent.last else [])]
        seen = visible + [t for e in agent.evidence for t in e.get("appeared", []) + e.get("disappeared", [])]
        summary, ops = reflect(self._chat(ctx), sess.name, goal, res.status, res.reason, agent.evidence,
                               sess.lessons(), visible)
        learned = sess.apply(ops, seen_texts=seen)
        sure = sess.apply(code_lessons(agent.evidence))  # kodun kesin bildiği: iki kez işe yaramayan tıklama
        row = sess.record(goal, res.status, res.steps, summary)
        verdict = reflex_verdict(res.status)
        lessons = (f"dersler: +{learned['add'] + sure['add']} yeni, {learned['confirm']} doğrulandı, "
                   f"{learned['contradict']} çelişti")
        return ToolResult(f"tur {row['turn']} ({sess.name})\n{res.report()}\nözet: {summary}\n{lessons}"
                          + ("\n" + verdict if verdict else ""), is_error=res.status == "error")


TOOLS = [Game()]
