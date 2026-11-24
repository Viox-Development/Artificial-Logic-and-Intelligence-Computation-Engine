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
        "Plays any game and learns its rules and interface while playing (strategy, cards, puzzles...). Keeps lessons "
        "across sessions; after each turn it looks at what really happened on screen and corrects itself.\n"
        "- start: choose the game and its window (the game must already be open). With no lessons it explores the "
        "rules and the first screen.\n"
        "- look: reads the game window at full resolution and gives the board as text (cards, pieces, numbers). Read "
        "the board with this, not with a computer screenshot.\n"
        "- play: look, pick one legal move with the known rules, make it on screen, compare what happened with what "
        "was expected and correct the rules. Empty goal: the best move.\n"
        "- turn: a concrete goal in the interface (e.g. 'end the turn', 'open the research screen').\n"
        "- note: a lasting note. status: lessons and the last turns. end: close.\n"
        "If a login, terms/privacy consent, purchase or ad screen comes up, don't get past it with another tool "
        "either: stop and leave it to the user."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["start", "look", "play", "turn", "note", "status", "end"],
                   "description": "start | look: read the board | play: one move | turn: interface goal | note | "
                                  "status | end."},
        "game": {"type": "string", "description": "For start: the game's name."},
        "title": {"type": "string", "description": "For start: part of the window title. Empty: the game's name."},
        "goal": {"type": "string", "description": "turn: a concrete goal to do on screen. play: what is wanted "
                                                  "(empty: the best legal move)."},
        "note": {"type": "string", "description": "For note: the information to add."},
        "max_steps": {"type": "integer", "description": "For turn/play: at most this many steps. Default 15."},
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
                raise ToolError("start needs game (the game's name)")
            sess = GameSession.open(alice_home(), game, title)
            ctx.state[STATE_KEY] = sess
            found = self._discover(ctx, sess) if not sess.lessons() and not sess.notes() else ""
            return ToolResult(f"game session: {sess.name} (window: '{sess.title}', {sess.turn} turns so far)\n"
                              f"lessons: {sess.lessons_path}\n{found}\n{sess.context()}")
        if sess is None:
            raise ToolError("no open game session: first action=start, game=<the game's name>")
        if action == "note":
            added = sess.add_note(note)
            return ToolResult("not eklendi" if added else "the note is empty or already there")
        if action == "status":
            return ToolResult(f"{sess.name}: {sess.turn} tur, {len(sess.lessons())} ders\n\n{sess.context()}")
        if action == "end":
            ctx.state.pop(STATE_KEY, None)
            return ToolResult(f"game session closed: {sess.name}, {sess.turn} turns, {len(sess.lessons())} lessons in total; "
                              f"all under {sess.root}")
        if action == "look":
            board = self._look(ctx, sess)
            return ToolResult(board or "could not read the board (no vision model)", is_error=not board)
        if action == "play":
            return self._play(ctx, sess, goal.strip() or DEFAULT_PLAY, max_steps)
        if action != "turn":
            raise ToolError(f"unknown action: {action}")
        if not goal.strip():
            raise ToolError("turn needs goal")
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
            return f"could not read the first screen ({e}); lessons will build up as you play\n"
        visible = [i.label for i in scr.items]
        board = self._read_board(ctx, sess, scr.image)
        found = discover(self._chat(ctx, "brain", 1500), sess.name, visible, board)
        n = sess.apply(found, source="prior", seen_texts=visible)["add"]
        identified = " ".join(str(found.get("identified") or "").split())[:120]
        if identified and identified.lower() != "menu":
            sess.add_note(f"Identified game: {identified}")
        return (f"the main model looked at the screen and recognised the game: {identified or 'unknown'}; {n} starting lessons "
                "(rules included, unverified)\n")

    def _read_board(self, ctx: ToolContext, sess: Any, image: Any, temperature: float = 0) -> str:
        from alice.reflex.game import look_board

        return look_board(self._chat(ctx, "vision", 1500, temperature), sess.name, image, sess.lessons())

    def _look(self, ctx: ToolContext, sess: Any) -> str:
        try:
            img = self._observer(ctx, sess, "").screen("").image
        except Exception as e:  # noqa: BLE001
            return f"(could not read the screen: {e})"
        return self._read_board(ctx, sess, img)

    def _look_twice(self, ctx: ToolContext, sess: Any) -> tuple[str, str]:
        """Read the same frame twice, the second time with some variety (at temperature 0 the answer repeats and
        adds nothing); the planner relies only on pieces both readings agree on."""
        try:
            img = self._observer(ctx, sess, "").screen("").image
        except Exception as e:  # noqa: BLE001
            return f"(could not read the screen: {e})", ""
        return self._read_board(ctx, sess, img), self._read_board(ctx, sess, img, SECOND_READ_TEMPERATURE)

    # ------------------------------------------------------------------ playing
    def _play(self, ctx: ToolContext, sess: Any, goal: str, max_steps: int) -> ToolResult:
        """Look -> plan (with the rules) -> Jev acts -> look again -> compare expected with actual, fix the rules."""
        from alice.reflex.game import plan_move

        if ctx.decider is None or ctx.router is None:
            raise ToolError("the decision engine is not configured")
        if not any(r.get("kind") == "rule" for r in sess.lessons()):
            # Started on a menu or game unidentified; if a board/map is visible now, the main model re-identifies it.
            self._discover(ctx, sess)
        board, board2 = self._look_twice(ctx, sess)
        recent = [f"turn {t['turn']}: {t['summary']}" for t in sess.turns()[-3:] if t.get("summary")]
        plan = plan_move(self._chat(ctx, "brain", 600), sess.name, goal, board, sess.lessons(), recent, board2)
        if not plan:
            return ToolResult(f"no move could be chosen (no board or plan).\nboard:\n{board[:BOARD_SHOWN]}", is_error=True)
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
            raise ToolError("the decision engine is not configured")
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
        verdict = reflex_verdict(status) or ("RESULT: the move did NOT go as expected (according to the board)."
                                             if status == "failed" else "")
        lessons = (f"lessons: +{learned['add'] + sure['add']} new, {learned['confirm']} confirmed, "
                   f"{learned['contradict']} contradicted, {learned['edit']} corrected")
        head = f"tur {row['turn']} ({sess.name})\n"
        if intent:
            head += (f"hamle: {intent['move']}\nneden: {intent.get('why', '')}\nbeklenen: {intent.get('expect', '')}"
                     "\n")
        tail = f"\nboard now:\n{board_after[:BOARD_SHOWN]}" if board_after else ""
        return ToolResult(f"{head}{res.report()}\nsummary: {summary}\n{lessons}" + ("\n" + verdict if verdict else "")
                          + tail, is_error=status == "error")


TOOLS = [Game()]
