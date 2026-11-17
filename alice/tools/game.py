"""Game session tool: play a game turn by turn with reflex, and learn it while playing (see alice/reflex/game.py)."""

from __future__ import annotations

import time
from typing import Any

from alice.tools.base import COMPUTER, READ, Tool, ToolContext, ToolError, ToolResult, obj

STATE_KEY = "game_session"
DEFAULT_PLAY = "Make the best legal next move."
BOARD_SHOWN = 2500  # characters: the board shown in the result
SECOND_READ_TEMPERATURE = 0.5  # the second, independent reading of the board
AFTER_MOVE_WAIT = 1.2  # seconds: after a move, before reading the board (animation)


class Game(Tool):
    name = "game"
    description = (
        "Herhangi bir oyunu oynar ve oynarken kurallarını, arayüzünü öğrenir (strateji, kart, bulmaca...). Dersleri "
        "oturumlar arası saklar, her turdan sonra ekranda gerçekten ne olduğuna bakıp düzeltir.\n"
        "- start: oyunu ve penceresini seç (oyun önceden açık olmalı). Ders yoksa kuralları ve ilk ekranı keşfeder.\n"
        "- look: oyun penceresini tam çözünürlükte okuyup tahtayı metin olarak verir (kartlar, taşlar, sayılar). "
        "Tahtayı computer ekran görüntüsüyle değil bununla oku.\n"
        "- play: bak, bilinen kurallarla tek bir yasal hamle seç, ekranda yap, beklenenle olanı kıyasla ve kuralları "
        "düzelt. goal boşsa en iyi hamle.\n"
        "- turn: arayüzde somut bir hedef (ör. 'end the turn', 'open the research screen').\n"
        "- note: kalıcı not. status: dersler ve son turlar. end: kapat.\n"
        "Giriş, şart/gizlilik onayı, satın alma ya da reklam ekranı çıkarsa başka araçla da geçme: dur, kullanıcıya "
        "bırak."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["start", "look", "play", "turn", "note", "status", "end"],
                   "description": "start | look: tahtayı oku | play: bir hamle | turn: arayüz hedefi | note | status | "
                                  "end."},
        "game": {"type": "string", "description": "start için oyunun adı."},
        "title": {"type": "string", "description": "start için pencere başlığından parça. Boş: oyunun adı."},
        "goal": {"type": "string", "description": "turn: ekranda yapılacak somut hedef. play: istenen (boş: en iyi "
                 "yasal hamle)."},
        "note": {"type": "string", "description": "note için eklenecek bilgi."},
        "max_steps": {"type": "integer", "description": "turn/play için en fazla adım. Varsayılan 15."},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return COMPUTER if args.get("action") in ("turn", "play", "start") else READ

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
        if action == "look":
            board = self._look(ctx, sess)
            return ToolResult(board or "tahta okunamadı (görüntü modeli yok)", is_error=not board)
        if action == "play":
            return self._play(ctx, sess, goal.strip() or DEFAULT_PLAY, max_steps)
        if action != "turn":
            raise ToolError(f"bilinmeyen action: {action}")
        if not goal.strip():
            raise ToolError("turn için goal gerekli")
        return self._turn(ctx, sess, goal, max_steps)

    # ------------------------------------------------------------------ models
    @staticmethod
    def _chat(ctx: ToolContext, role: str = "fast", max_tokens: int = 700, temperature: float = 0):
        if ctx.router is None:
            return None

        def chat(messages: list[dict[str, Any]]) -> str:
            return ctx.router.chat(role, messages, temperature=temperature, max_tokens=max_tokens).text

        return chat

    def _observer(self, ctx: ToolContext, sess: Any, goal: str) -> Any:
        from alice.reflex.observers import DesktopObserver

        if sess.observer is None:
            sess.observer = DesktopObserver(ctx, sess.title, goal, diff=True)
        return sess.observer

    def _discover(self, ctx: ToolContext, sess: Any) -> str:
        """Identify the game: the vision model reads the screen; the main model works out what is being played and
        writes the rules and UI (unverified). Jev gets the rules first at every step; play verifies or fixes them."""
        from alice.reflex.game import discover

        try:
            scr = self._observer(ctx, sess, "").screen("")
        except Exception as e:  # noqa: BLE001 - if the screen can't be read, start without discovery
            return f"ilk ekran okunamadı ({e}); dersler oynadıkça birikecek\n"
        visible = [i.label for i in scr.items]
        board = self._read_board(ctx, sess, scr.image)
        found = discover(self._chat(ctx, "brain", 1500), sess.name, visible, board)
        n = sess.apply(found, source="prior", seen_texts=visible)["add"]
        identified = " ".join(str(found.get("identified") or "").split())[:120]
        if identified and identified.lower() != "menu":
            sess.add_note(f"Identified game: {identified}")
        return (f"ana model ekrana bakıp oyunu tanıdı: {identified or 'bilinmiyor'}; {n} başlangıç dersi "
                "(kurallar dahil, doğrulanmamış)\n")

    def _read_board(self, ctx: ToolContext, sess: Any, image: Any, temperature: float = 0) -> str:
        from alice.reflex.game import look_board

        return look_board(self._chat(ctx, "vision", 1500, temperature), sess.name, image, sess.lessons())

    def _look(self, ctx: ToolContext, sess: Any) -> str:
        try:
            img = self._observer(ctx, sess, "").screen("").image
        except Exception as e:  # noqa: BLE001
            return f"(ekran okunamadı: {e})"
        return self._read_board(ctx, sess, img)

    def _look_twice(self, ctx: ToolContext, sess: Any) -> tuple[str, str]:
        """Read the same frame twice, the second time with some variety (at temperature 0 the answer repeats and
        adds nothing); the planner relies only on pieces both readings agree on."""
        try:
            img = self._observer(ctx, sess, "").screen("").image
        except Exception as e:  # noqa: BLE001
            return f"(ekran okunamadı: {e})", ""
        return self._read_board(ctx, sess, img), self._read_board(ctx, sess, img, SECOND_READ_TEMPERATURE)

    # ------------------------------------------------------------------ playing
    def _play(self, ctx: ToolContext, sess: Any, goal: str, max_steps: int) -> ToolResult:
        """Look -> plan (with the rules) -> Jev acts -> look again -> compare expected with actual, fix the rules."""
        from alice.reflex.game import plan_move

        if ctx.decider is None or ctx.router is None:
            raise ToolError("karar motoru yapılandırılmamış")
        if not any(r.get("kind") == "rule" for r in sess.lessons()):
            # Started on a menu or game unidentified; if a board/map is visible now, the main model re-identifies it.
            self._discover(ctx, sess)
        board, board2 = self._look_twice(ctx, sess)
        recent = [f"turn {t['turn']}: {t['summary']}" for t in sess.turns()[-3:] if t.get("summary")]
        plan = plan_move(self._chat(ctx, "brain", 600), sess.name, goal, board, sess.lessons(), recent, board2)
        if not plan:
            return ToolResult(f"hamle seçilemedi (tahta ya da plan yok).\ntahta:\n{board[:BOARD_SHOWN]}", is_error=True)
        # Rules the planner relies on but that aren't known yet are stored as unverified rules; the move's result
        # verifies or drops them. Rules come from the board, not the game name: discovery under "Solitaire Collection"
        # wrote no rules at all.
        new_rules = [{"trigger": "", "do": str(r), "kind": "rule"} for r in plan.get("rules_new") or [] if str(r).strip()]
        if new_rules:
            sess.apply({"add": new_rules}, source="prior")
        return self._turn(ctx, sess, plan["move"], max_steps, intent=plan)

    def _turn(self, ctx: ToolContext, sess: Any, goal: str, max_steps: int,
              intent: dict[str, Any] | None = None) -> ToolResult:
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
        agent.stop_after_change = intent is not None  # move = first screen-changing action; verify via the board
        why =f"\n\nThis move was chosen because: {intent.get('why', '')}\nExpected: {intent.get('expect', '')}" \
            if intent else ""
        res = agent.run(goal, max_steps=max_steps,
                        notes=lambda scr: sess.context([i.label for i in scr.items]) + why)
        board_after = ""
        if intent:
            # Check visually (cards/stones, not text) whether the expected happened. Let animations finish first:
            # in Solitaire a card was drawn but read as "unchanged" before the table-opening animation ended.
            time.sleep(AFTER_MOVE_WAIT)
            board_after = self._look(ctx, sess)
        visible = [i.label for i in (agent.last.items if agent.last else [])]
        seen = visible + [t for e in agent.evidence for t in e.get("appeared", []) + e.get("disappeared", [])]
        summary, ops = reflect(self._chat(ctx), sess.name, goal, res.status, res.reason, agent.evidence,
                               sess.lessons(), visible, intent=intent, board_after=board_after)
        learned = sess.apply(ops, seen_texts=seen)
        sure = sess.apply(code_lessons(agent.evidence))  # what the code knows for certain: clicks that failed twice
        status = res.status
        if intent and ops.get("expected_happened") in (True, False):
            # The board, not the click count, decides whether the move happened (4♣ moved on the first click but
            # was counted as "failed" and a wrong rule was written).
            status = "done" if ops["expected_happened"] else "failed"
        row = sess.record(goal, status, res.steps, summary)
        verdict = reflex_verdict(status) or ("SONUÇ: hamle beklendiği gibi OLMADI (tahtaya göre)."
                                             if status == "failed" else "")
        lessons = (f"dersler: +{learned['add'] + sure['add']} yeni, {learned['confirm']} doğrulandı, "
                   f"{learned['contradict']} çelişti, {learned['edit']} düzeltildi")
        head = f"tur {row['turn']} ({sess.name})\n"
        if intent:
            head += (f"hamle: {intent['move']}\nneden: {intent.get('why', '')}\nbeklenen: {intent.get('expect', '')}"
                     "\n")
        tail = f"\ntahta şimdi:\n{board_after[:BOARD_SHOWN]}" if board_after else ""
        return ToolResult(f"{head}{res.report()}\nözet: {summary}\n{lessons}" + ("\n" + verdict if verdict else "")
                          + tail, is_error=status == "error")


TOOLS = [Game()]
