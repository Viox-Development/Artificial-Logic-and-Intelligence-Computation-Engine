"""Oyun oturumu aracı: bir oyunu turlar boyunca reflex'le oynamak (bkz. alice/reflex/game.py)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, READ, Tool, ToolContext, ToolError, ToolResult, obj

STATE_KEY = "game_session"


class Game(Tool):
    name = "game"
    description = (
        "Bir oyunu turlar boyunca oynar (sıra tabanlı strateji, kart, bulmaca; deneme oyunu Unciv). Tek reflex "
        "çağrısından farkı: oyunun notlarını (arayüz, kurallar, strateji) her tura taşır, ekranın sadece değişen "
        "kısımlarını yeniden okur ve yeni çıkanları işaretler, her turdan sonra kısa özet yazar.\n"
        "- start: oyunu ve penceresini seç (oyun önceden app/windows ile açık olmalı); notları ve son turları "
        "döndürür.\n"
        "- turn: goal'u ekranda yap (ör. 'end the turn', 'pick Pottery as research'). Büyük hedefi küçük turlara böl.\n"
        "- note: öğrendiğin kalıcı bir bilgiyi notlara ekle (İngilizce, kısa): kural, arayüzde neyin nerede olduğu, "
        "strateji.\n"
        "- status: notlar ve son tur özetleri. end: oturumu kapat."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["start", "turn", "note", "status", "end"],
                   "description": "start: oturumu aç | turn: bir hedefi oyna | note: not ekle | status | end."},
        "game": {"type": "string", "description": "start için oyunun adı, ör. 'Unciv'."},
        "title": {"type": "string", "description": "start için pencere başlığından parça. Boş: oyunun adı."},
        "goal": {"type": "string", "description": "turn için bu turda ekranda yapılacak kısa ve somut hedef."},
        "note": {"type": "string", "description": "note için eklenecek bilgi."},
        "max_steps": {"type": "integer", "description": "turn için en fazla adım. Varsayılan 15."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return COMPUTER if args.get("action") == "turn" else READ

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
            return ToolResult(f"oyun oturumu: {sess.name} (pencere: '{sess.title}', şimdiye kadar {sess.turn} tur)\n"
                              f"notlar: {sess.notes_path}\n\n{sess.context()}")
        if sess is None:
            raise ToolError("açık oyun oturumu yok: önce action=start, game=<oyunun adı>")
        if action == "note":
            added = sess.add_note(note)
            return ToolResult("not eklendi" if added else "not boş ya da zaten var")
        if action == "status":
            return ToolResult(f"{sess.name}: {sess.turn} tur\n\n{sess.context()}")
        if action == "end":
            ctx.state.pop(STATE_KEY, None)
            return ToolResult(f"oyun oturumu kapandı: {sess.name}, toplam {sess.turn} tur; notlar ve özetler "
                              f"{sess.root} altında")
        if action != "turn":
            raise ToolError(f"bilinmeyen action: {action}")
        if not goal.strip():
            raise ToolError("turn için goal gerekli")
        return self._turn(ctx, sess, goal, max_steps)

    def _turn(self, ctx: ToolContext, sess: Any, goal: str, max_steps: int) -> ToolResult:
        from alice.config import alice_home
        from alice.reflex.agent import ScreenAgent, make_translator
        from alice.reflex.game import summarize_turn
        from alice.reflex.loop import make_text_writer
        from alice.reflex.observers import DesktopObserver
        from alice.tools.meta import reflex_verdict

        if ctx.decider is None or ctx.router is None:
            raise ToolError("karar motoru yapılandırılmamış")
        if sess.observer is None:
            sess.observer = DesktopObserver(ctx, sess.title, goal, diff=True)
        agent = ScreenAgent(sess.observer, ctx.decider, make_text_writer(ctx.router), make_translator(ctx.router),
                            emit=ctx.emit, trace_path=alice_home() / "reflex-trace.jsonl")
        res = agent.run(goal, max_steps=max_steps, notes=sess.context())
        new = [i.label for i in (agent.last.items if agent.last else []) if i.fresh][:15]

        def chat(messages: list[dict[str, str]]) -> str:
            return ctx.router.chat("fast", messages, temperature=0, max_tokens=300).text

        summary = summarize_turn(chat, goal, res.status, res.log, new)
        row = sess.record(goal, res.status, res.steps, summary)
        verdict = reflex_verdict(res.status)
        return ToolResult(f"tur {row['turn']} ({sess.name})\n{res.report()}\nözet: {summary}"
                          + ("\n" + verdict if verdict else ""), is_error=res.status == "error")


TOOLS = [Game()]
