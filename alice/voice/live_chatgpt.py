"""ALICE Live (ChatGPT Realtime): speech-to-speech live conversation over CLIProxyAPI.

CLIProxyAPI serves an OpenAI Realtime-compatible endpoint (`/v1/realtime`) backed by a ChatGPT (Codex) account.
This module uses that endpoint: microphone audio is streamed as 24 kHz PCM, the model's voice is played back at
the same speed, a reply is generated once the user goes quiet (server-side VAD), and playback stops if the user
interrupts. ALICE's tools are called via function calling (through the same permission/approval path).

Provides the same interface as Gemini Live (alice/voice/live.py): VoiceAssistant selects it with `make_live`.
"""

from __future__ import annotations

import array
import base64
import json
import re
import queue
import threading
import time
from typing import Any, Callable

from alice.i18n import speech_language
from alice.tools.base import ToolResult
from alice.voice.live import AudioOut, LiveError, LiveUI, sanitize_schema

REALTIME_PROMPT = """# Live voice conversation
You are talking with the user live, by voice. ALWAYS SPEAK TURKISH: natural, warm and short (usually 1-2 sentences).
Pronunciation: speak like a young woman whose native language is Turkish and who lives in Istanbul; no foreign accent.
Say the Turkish vowels (ı, ö, ü) and the r like a Turk, put the stress near the end of the word as in Turkish, and end
the sentence in a flat, relaxed tone at a natural pace.
Don't read out lists, code, long paths or numbers; if needed, say you put it on the screen.
Do one-step jobs (play music/video, open something on YouTube, open an app or a site, volume, reminder, timer) right
away with the relevant tool; don't call `think` for these, it is far too slow.
When information, an explanation, a plan, code, a calculation, something current or a multi-step job is asked for,
call the `think` tool. First announce it in one short sentence ("Bir bakayım."), then tell the result `think`
returned in your own words, short and natural. Don't read the result out as is. Don't say "yaptım" (I did it) before
you have seen the tool result.
The text you hear comes from speech recognition and words may be garbled (e.g. your name "Alice" may be written as
"Aisla" or "Alis"). If you understood the gist of the request, ignore the meaningless word and do it without asking;
ask one short question only if you really didn't understand.
If `think` asked a question (information the strong model needs from you), pass that question to the user as is and
wait for the answer; give the answer to the `think` tool again as a request. If a job is in progress, continue the
same conversation.
If you call a tool without saying anything, the user hears a short "Bir saniye."; don't repeat it after the tool
result, say the result directly.
Never make up numbers or states the tools didn't give; if you don't know, say so.
The microphone also hears games, videos and other people talking; don't answer sentences that weren't said to you,
that are meaningless or that aren't Turkish: stay silent. The user also talks with friends on Discord or in a game: if
the sentence is addressed to someone else (a name, "gel buraya", "bak şimdi") or is a remark about the game said to
themself, make no sound at all. If it continues the conversation (the answer to the question just asked, "evet",
"aç onu"), answer. If the user says "sus", "yeter", "sana demedim" or "sana demiyorum", say nothing: not even "tamam".
Always speak Turkish."""

THINK_TOOL = {
    "type": "function",
    "name": "think",
    "description": ("Hand information, explanations, plans, code, calculations, current information or jobs done with "
                    "ALICE's tools to the strong model. Tell the user the result in short Turkish sentences; if a "
                    "question comes back, pass it to the user."),
    "parameters": {"type": "object", "properties": {
        "request": {"type": "string", "description": "The user's request; complete, as said"}},
        "required": ["request"]},
}

# Fast tools the live model calls directly (config: [voice] live_realtime_tools, "*" = all).
REALTIME_TOOLS = ("youtube", "media", "windows", "weather", "notes", "now", "spotify", "memory")

# Voices accepted by Codex Realtime (2026-11-06, from the server's "Supported values" error).
REALTIME_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")

HUSH_WORDS = ("sus", "sussana", "susun", "şşt", "boşver")  # silences ALICE even if the word is anywhere in the sentence

_OPENAI_TYPES = {"OBJECT": "object", "STRING": "string", "INTEGER": "integer", "NUMBER": "number",
                 "BOOLEAN": "boolean", "ARRAY": "array", "NULL": "null"}


def openai_schema(schema: Any) -> Any:
    """Convert an ALICE tool schema to the lowercase JSON Schema that OpenAI Realtime expects.

    ALICE's internal schemas use uppercase types for Gemini (`STRING`, `INTEGER`, `OBJECT`); the Realtime API
    rejects them with `invalid_function_parameters` and the whole session update was being dropped.
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key == "type" and isinstance(value, str):
                out["type"] = _OPENAI_TYPES.get(value.upper(), value.lower())
            elif key == "type" and isinstance(value, list):
                types = [_OPENAI_TYPES.get(str(t).upper(), str(t).lower()) for t in value if t != "null"]
                out["type"] = types[0] if len(types) == 1 else types
            elif key == "nullable":
                continue  # The OpenAI schema doesn't know "nullable"; the field isn't required anyway
            else:
                out[key] = openai_schema(value)
        return out
    if isinstance(schema, list):
        return [openai_schema(item) for item in schema]
    return schema


def _close(pre: dict[str, Any]) -> None:
    """Close a prepared (unused) connection; if it is still being prepared, wait for that to finish."""
    t = pre.get("thread")
    if t is not None and t.is_alive():
        t.join(RealtimeSession.PREFETCH_WAIT)
    ws = pre.get("ws")
    if ws is not None:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def _is_open(ws: Any) -> bool:
    """The pre-warmed connection may have been closed by the server (timeout, proxy restarted)."""
    try:
        return getattr(ws.protocol.state, "name", "OPEN") == "OPEN"
    except Exception:  # noqa: BLE001 - if the state can't be read, try anyway; converse will raise if it fails
        return True


def resample_16_to_24(pcm: bytes) -> bytes:
    """16 kHz PCM16 -> 24 kHz PCM16 (linear interpolation). Good enough and fast for small frames."""
    if not pcm:
        return b""
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n = len(a)
    if n == 0:
        return b""
    out = array.array("h", [0]) * (n * 3 // 2)
    for i in range(len(out)):
        x = i * 2 / 3
        j = int(x)
        frac = x - j
        v = a[j] if j + 1 >= n else a[j] * (1 - frac) + a[j + 1] * frac
        out[i] = int(v)
    return out.tobytes()


class RealtimeSession:
    """ChatGPT Realtime session (via CLIProxyAPI). If `available` is False, the Gemini/legacy path is used."""

    TOOL_TIMEOUT = 300.0
    PREFETCH_WAIT = 10.0

    def __init__(self, cfg, agent, log: Callable[[str], None] = lambda s: None,
                 out_factory: Callable[[], Any] = AudioOut, connect: Callable[..., Any] | None = None):
        v = cfg.data.get("voice", {})
        self.cfg = cfg
        self.agent = agent
        self.log = log
        self.model = str(v.get("live_realtime_model") or "gpt-realtime-2.1")
        self.voice = str(v.get("live_realtime_voice") or "marin").strip().lower()
        if self.voice not in REALTIME_VOICES:
            # An invalid voice name (e.g. "quip" from the ChatGPT app) made the server drop the whole session.update:
            # the instructions and tools weren't applied either.
            log(f"realtime: '{self.voice}' sesi desteklenmiyor, marin kullanılıyor")
            self.voice = "marin"
        self.idle = min(float(v.get("live_idle", 5)), 5)
        self.silence_ms = int(v.get("live_silence_ms", 600))
        # When using speakers, ALICE's own voice can leak back into the mic. Realtime VAD mistakes it for a new user
        # turn and cancels the reply in progress, so by default we don't send the mic to the server during a reply.
        # Headphone users can turn on the old "interrupt by talking over it" behavior.
        self.echo_guard = bool(v.get("live_realtime_echo_guard", True))
        self.tools_on = bool(v.get("live_tools", True))
        # The fast voice model is weak: the think tool hands knowledge/plan/code/tool work to the stronger model.
        self.think = bool(v.get("live_think", True))
        # think's model order (same setting as the Gemini path). It used to use the voice agent's own chain:
        # when the first model stalled (Nov 6: gemini-flash-latest ReadTimeout), a single "open video" took ~95 s.
        self.think_models = [str(r) for r in v.get("live_think_models") or []]
        # Only the fast single-step tools go to the live model; the rest go to the strong model via think. The
        # 69 tool schemas (~112 KB) added ~1 s to each session.update and to first audio (Nov 6 measurement).
        tools = v.get("live_realtime_tools", REALTIME_TOOLS)
        self.allowed_tools: set[str] | None = None if tools == "*" or tools == ["*"] else {str(t) for t in tools}
        # ChatGPT hears the audio itself: we send audio rather than waiting (≤1.5 s) for the local Whisper transcript
        # and sending text that may be mis-transcribed. Old behavior: live_realtime_text_hint = true.
        self.prefers_audio = not bool(v.get("live_realtime_text_hint", False))
        # ChatGPT's own transcription: heard speech goes to logs and history; "Alice sus" works on voice too.
        self.transcribe = str(v.get("live_realtime_transcribe", "gpt-4o-mini-transcribe") or "")
        self.language = speech_language(v.get("language", "auto"))
        # After a reply, prepare the next session in the background so connect + setup (≈2 s) doesn't delay speech.
        self.prewarm = bool(v.get("live_realtime_prewarm", True))
        self._think_lock = threading.Lock()
        self.gated = False
        self.hushed = False
        self.last_active = 0.0
        self.single_turn = False
        self.declined = False  # last follow-up wasn't addressed to ALICE (Jev): no follow-up window opens
        # Barge-in: if a frame arriving while ALICE speaks is verified as the user's voice (voiceprint, VoiceAssistant
        # settings), the reply is cut off. Without verification it stays off because of echo (echo_guard).
        self.barge_ok: Callable[[], bool] = lambda: False
        # Phone call (alice/app/call.py): show the heard and spoken text on screen.
        self.on_heard: Callable[[str], None] | None = None
        self.on_said: Callable[[str], None] | None = None
        self.hud_on = True  # a phone call must not play the computer's HUD
        self.announce: Callable[[str], None] | None = None
        self.on_correction: Callable[[str], None] | None = None
        self._out_factory = out_factory
        self._connect = connect
        self._send_update = False
        self._out = None
        self._ws = None
        self._stop = None
        self._tools_q: queue.Queue | None = None
        self._pre: dict[str, Any] = {}
        self._pre_lock = threading.Lock()
        self._proxy = self._read_proxy()
        # Each "Alice" (every sentence on Discord/in games) opens a new session; unless the previous conversation is
        # added to the instructions, each one was heard with no context ("herhangi bir şey açarsan olur" -> what?).
        self.history_minutes = float(v.get("live_history_minutes", 30))
        self._history: list[tuple[float, str, str]] = []

    # ------------------------------------------------------------------ setup (preparation)
    @staticmethod
    def _read_proxy() -> dict[str, str] | None:
        """Local CLIProxyAPI settings: base address and client key (None if absent)."""
        try:
            from alice.app import cliproxy

            s = cliproxy.settings()
            if not s.get("installed"):
                return None
            key = ""
            for line in (s["dir"] / "keys.txt").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("api_key="):
                    key = line.split("=", 1)[1].strip()
            if not key:
                return None
            return {"base": f"http://127.0.0.1:{s['port']}", "key": key}
        except Exception:  # noqa: BLE001 - silently ignore it if the proxy isn't installed
            return None

    @property
    def available(self) -> bool:
        if self._connect is not None:
            return True
        if not self._proxy:
            return False
        try:
            from websockets.sync.client import connect  # noqa: F401
        except ImportError:
            return False
        return bool(self.model)

    @property
    def playing(self) -> bool:
        out = getattr(self, "_out", None)
        return bool(out is not None and getattr(out, "playing", False))

    def _instructions(self) -> str:
        base = ""
        try:
            base = str(self.agent.messages[0].get("content") or "")
        except Exception:  # noqa: BLE001
            pass
        refresh = getattr(self.agent, "refresh_memory", None)
        if callable(refresh):
            try:
                refresh(force=True)
            except Exception:  # noqa: BLE001
                pass
        history = self.history_text()
        tail = ("\n\n# Recent conversation (the same chat, continued; read short answers in its light)\n" + history
                if history else "")
        try:
            # The system prompt's date goes stale in a long-running process: a phone call said "bugün 6 Kasım Cuma"
            # (it was actually Saturday, 7 November). The text path adds the clock (with_clock) to every sentence;
            # the live path should too.
            from alice.voice.assistant import with_clock

            tail = "\n\n# Now\n" + with_clock("").strip() + tail
        except Exception:  # noqa: BLE001
            pass
        # Don't let trimming cut the history and live-conversation rules: trim the base system instructions instead.
        room = 12000 - len(REALTIME_PROMPT) - len(tail) - 2
        return base[:max(0, room)] + "\n\n" + REALTIME_PROMPT + tail

    def history_text(self, limit: int = 8) -> str:
        from datetime import datetime

        cutoff = time.time() - self.history_minutes * 60
        lines = []
        for ts, user, reply in [h for h in self._history if h[0] >= cutoff][-limit:]:
            line = f"- {datetime.fromtimestamp(ts):%H:%M} Kullanıcı: {user[:300]}"
            if reply:
                line += f" / Sen: {reply[:300]}"
            lines.append(line)
        return "\n".join(lines)

    def _remember(self, heard: str, said: str, tools: list[str]) -> None:
        heard, said = heard.strip(), said.strip()
        if heard or said or tools:
            extra = f" araçlar: {', '.join(tools)}" if tools else ""
            self.log(f"realtime duyulan: {heard[:300]} | cevap: {said[:400]}{extra}")
        if heard:
            self._history = (self._history + [(time.time(), heard, said)])[-20:]
        elif said and self._history:
            ts, user, before = self._history[-1]
            self._history[-1] = (ts, user, (before + " " + said).strip())

    def _tool_specs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [THINK_TOOL] if self.think else []
        if not self.tools_on:
            return out
        tools = getattr(self.agent, "tools", None)
        if isinstance(tools, dict):
            raw = [t.spec() for name, t in tools.items() if not name.startswith("mcp__") and name != "load_tools"
                   and (self.allowed_tools is None or name in self.allowed_tools)]
        else:
            try:
                raw = self.agent.tool_specs()
            except Exception:  # noqa: BLE001
                return out
        for spec in raw:
            f = spec.get("function", spec)
            if not f.get("name"):
                continue
            item: dict[str, Any] = {"type": "function", "name": f["name"],
                                    "description": str(f.get("description") or "")[:1000]}
            params = openai_schema(sanitize_schema(f.get("parameters") or {}))
            if params.get("properties"):
                item["parameters"] = params
            out.append(item)
        return out

    def _session_config(self) -> dict[str, Any]:
        session: dict[str, Any] = {
            "type": "realtime",
            "model": self.model,
            "instructions": self._instructions(),
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000},
                          "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                             "prefix_padding_ms": 300, "silence_duration_ms": self.silence_ms,
                                             "interrupt_response": not self.echo_guard}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": self.voice},
            },
        }
        if self.transcribe:
            session["audio"]["input"]["transcription"] = {"model": self.transcribe, "language": self.language}
        tools = self._tool_specs()
        if tools:
            session["tools"] = tools
            session["tool_choice"] = "auto"
        return {"expires_after": {"anchor": "created_at", "seconds": 900}, "session": session}

    def _secret(self) -> str:
        if not self._proxy:
            raise LiveError("yerel Claude Code proxy'si (CLIProxyAPI) ayarlı değil")
        # CLIProxyAPI's /v1/realtime/client_secrets endpoint accepts a 64 KiB body; when ALICE's instructions and
        # tool schemas exceed it, it returns 413 and the live session never opened (the long, robotic fallback path).
        # Instead, connect with the proxy API key and update the session over the WebSocket: same session,
        # no size limit.
        self._send_update = True
        return str(self._proxy["key"])

    def _ws_url(self) -> str:
        base = (self._proxy or {}).get("base", "http://127.0.0.1:8317")
        ws = base.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws}/v1/realtime?model={self.model}"

    # ------------------------------------------------------------------ warm-up
    WARM_MAX = 60.0  # s: a warmed-up session older than this is closed (an unused connection must not stay open)

    def _open(self) -> Any:
        secret = self._secret()
        if self._connect is not None:
            return self._connect(self._ws_url(), secret)
        from websockets.sync.client import connect

        try:
            return connect(self._ws_url(), additional_headers={"Authorization": f"Bearer {secret}"},
                           open_timeout=10, max_size=None)
        except Exception as e:  # noqa: BLE001
            raise LiveError(f"realtime bağlanamadı: {e}") from e

    def prefetch(self, owner: Any = None) -> None:
        """Warm up the session in advance: connect, get session.created, send the settings (instructions, tools,
        voice). Runs alongside the wake check and after every answer, so these ~2 s are not waited for when the
        conversation starts (measured: connection ~0.9 s + settings ~1 s)."""
        if not self.available:
            return
        with self._pre_lock:
            if self._pre:
                return
            pre: dict[str, Any] = {"owner": owner, "t": time.monotonic()}

            def run() -> None:
                try:
                    ws = self._open()
                    ws.recv(timeout=10)  # session.created
                    ws.send(json.dumps({"type": "session.update", "session": self._session_config()["session"]},
                                       ensure_ascii=False))
                    pre["ws"] = ws
                except Exception as e:  # noqa: BLE001
                    pre["error"] = e

            def expire() -> None:
                with self._pre_lock:
                    if self._pre is not pre:
                        return
                    self._pre = {}
                _close(pre)

            pre["thread"] = threading.Thread(target=run, daemon=True, name="alice-live-rt-pre")
            self._pre = pre
            pre["thread"].start()
            timer = threading.Timer(self.WARM_MAX, expire)
            timer.daemon = True
            timer.start()

    def discard(self, owner: Any = None) -> None:
        """No wake word after all: close the connection opened for that sentence. Don't touch anyone else's session
        (e.g. one warmed up after an answer)."""
        with self._pre_lock:
            if not self._pre or (owner is not None and self._pre.get("owner") is not owner):
                return
            pre, self._pre = self._pre, {}
        threading.Thread(target=_close, args=(pre,), daemon=True).start()

    def _take(self) -> tuple[Any, bool]:
        """(connection, warmed up?). If warmed up, the settings were already sent."""
        with self._pre_lock:
            pre, self._pre = self._pre, {}
        if pre:
            pre["thread"].join(self.PREFETCH_WAIT)
            ws = pre.get("ws")
            if ws is not None and time.monotonic() - pre["t"] < self.WARM_MAX and _is_open(ws):
                return ws, True
            _close(pre)
            if pre.get("error"):
                self.log(f"realtime ön hazırlık başarısız: {pre['error']}")
        return self._open(), False

    # ------------------------------------------------------------------ main flow
    def converse(self, first_text: str, frames: "queue.Queue[bytes]", stop: threading.Event,
                 first_audio: bytes = b"", hint: Callable[[], str | None] | None = None,
                 ended: float | None = None, need_wake: bool = False, check_addressed: bool = False) -> None:
        """Stream mic frames (16 kHz, 30 ms) to the model, play the answer, run the tools. Returns on silence."""
        if hint is not None and not first_text and not self.prefers_audio:
            try:
                first_text = hint() or ""
            except Exception:  # noqa: BLE001
                first_text = ""
        t0 = time.monotonic()
        ws, warm = self._take()
        out = self._out_factory()
        self._ws, self._out, self._stop = ws, out, stop
        ui = LiveUI(self.agent.ui)
        old_ui, self.agent.ui = self.agent.ui, ui
        self.hushed = False
        state = {"active": time.monotonic(), "heard": "", "said": "", "calls": {}, "done": False,
                 "answered": False, "want_response": False, "output_until": 0.0,
                 "echo_notice": False, "finish": False, "said_parts": [], "tools": [],
                 # A follow-up sentence in Discord: an answer is produced only if Jev says "it was said to ALICE" (addressed.py).
                 "gate": "pending" if check_addressed else "open", "gate_until": time.monotonic() + 8.0,
                 "check": check_addressed, "muted": False, "responding": False, "barged": False}
        self.declined = False
        self.log(f"realtime oturumu açıldı [{self.model}] {'ısıtılmış' if warm else 'yeni bağlantı'} "
                 f"{time.monotonic() - t0:.2f} sn")
        self._hud("listen", "")

        def touch() -> None:
            state["active"] = time.monotonic()
            self.last_active = state["active"]

        def send(obj: dict[str, Any]) -> None:
            try:
                ws.send(json.dumps(obj, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                raise LiveError(f"realtime gönderilemedi: {e}") from e

        def send_pcm(pcm: bytes) -> None:
            if pcm:
                send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})

        def say_text(text: str) -> None:
            send({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}]}})
            send({"type": "response.create", "response": {"output_modalities": ["audio"]}})
            touch()

        tool_thread: threading.Thread | None = None
        try:
            if not warm:
                ws.recv()  # session.created (or an error); the details are not needed
                if self._send_update:
                    # Instructions, tools and voice: sent after the session opens instead of in the HTTP body.
                    send({"type": "session.update", "session": self._session_config()["session"]})
            if state["gate"] == "pending":
                td = self._session_config()["session"]["audio"]["input"]["turn_detection"]
                send({"type": "session.update", "session": {"type": "realtime", "audio": {"input": {
                    "turn_detection": {**td, "create_response": False}}}}})
            if first_text:
                say_text(first_text)
                pending_audio = b""
            else:
                pending_audio = resample_16_to_24(first_audio)
            while not stop.is_set() and not state["done"]:
                while True:  # mikrofondan biriken kareler (16 kHz -> 24 kHz)
                    try:
                        frame = frames.get_nowait()
                    except queue.Empty:
                        break
                    if frame:
                        # While audio plays we would only be feeding the speaker echo in the mic back to Realtime.
                        # That was the real loop that made VAD open a new turn and drop the response halfway.
                        # Dropping the frames here also keeps the old audio from being handled as a new request
                        # once speech ends. ``output_until`` covers the short gaps between network packets.
                        if self.echo_guard and (self.playing or time.monotonic() < state["output_until"]):
                            if not (any(frame) and self.barge_ok()):
                                continue
                            if not state["barged"]:
                                # The voice print confirmed it: the user is interrupting ALICE (every frame arriving while
                                # it spoke used to be dropped; neither cutting in nor "Alice sus" worked, 7 November). Once
                                # per answer: after the audio is cut the player looks "playing" for a moment, and a cancel
                                # was sent again on every frame (dozens of response_cancel_not_active).
                                self.log("realtime: kullanıcı araya girdi, ALICE susuyor")
                                out.clear()
                                if state["responding"]:
                                    send({"type": "response.cancel"})
                                state.update(output_until=0.0, finish=False, muted=state["responding"],
                                             barged=True)
                                if state["check"]:  # Discord: let Jev check whether the new sentence was also said to ALICE
                                    state.update(gate="pending", gate_until=time.monotonic() + 8.0)
                        send_pcm(resample_16_to_24(frame))
                        # Silent frames must not keep the session open: it closes if nobody speaks for 5 s.
                        if any(frame):
                            touch()
                if pending_audio:
                    send_pcm(pending_audio)
                    pending_audio = b""
                try:
                    raw = ws.recv(timeout=0.05)
                except TimeoutError:
                    raw = None
                except Exception as e:  # noqa: BLE001 - the connection dropped
                    raise LiveError(f"realtime bağlantısı koptu: {e}") from e
                if raw is None:
                    if self.playing:
                        # The silence window starts after ALICE stops talking. The model sends audio faster than real
                        # time, so the last chunk arrives long before; the session used to close as soon as the audio
                        # arrived, and the user's reply went to a new (context-free, slow) session.
                        touch()
                        continue
                    if tool_thread is not None and tool_thread.is_alive():
                        # A tool/think is running: don't count silence. The session used to close after 12 s, and think's
                        # result went to the closed connection and was lost ("eee açacaktın" = well, you were going to open it).
                        touch()
                        continue
                    if state["gate"] == "pending" and time.monotonic() > state["gate_until"]:
                        self.log("realtime: devam cümlesinin dökümü gelmedi, cevap yok")
                        self.declined = True
                        break
                    if state["finish"]:
                        break  # single-answer turn (Discord/game): the answer played to the end
                    if time.monotonic() - state["active"] > self.idle:
                        self.log("realtime: sessizlik, oturum kapanıyor")
                        break
                    continue
                ev = self._decode(raw)
                kind = ev.get("type", "")
                if kind == "response.created":
                    state.update(muted=False, responding=True, barged=False)  # a new answer (also after an interruption)
                elif kind in ("response.output_audio.delta", "response.audio.delta") and state["muted"]:
                    continue  # chunks of the interrupted answer still on the way
                elif kind in ("response.output_audio.delta", "response.audio.delta"):
                    try:
                        out.put(base64.b64decode(ev.get("delta", "")))
                    except Exception:  # noqa: BLE001
                        pass
                    state["output_until"] = time.monotonic() + 0.35
                    if not state["answered"]:
                        # Latency: from when the user stopped talking (otherwise from the session request) to the first sound.
                        start = ended if isinstance(ended, (int, float)) and ended > 0 else t0
                        self.log(f"realtime ilk ses: {time.monotonic() - start:.2f} sn")
                    state["answered"] = True
                    touch()
                elif kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
                    state["said"] += ev.get("delta", "")
                    touch()
                elif kind == "response.output_audio_transcript.done" or kind == "response.audio_transcript.done":
                    # "Bir bakayım." (let me look) before a tool and the answer after it are separate answers: log both.
                    part = str(ev.get("transcript") or state["said"]).strip()
                    if part:
                        state["said_parts"].append(part)
                        if self.on_said is not None:
                            try:
                                self.on_said(part)
                            except Exception:  # noqa: BLE001
                                pass
                    state["said"] = ""
                elif kind == "conversation.item.input_audio_transcription.completed":
                    text = str(ev.get("transcript") or "").strip()
                    if text:
                        state["heard"] += " " + text
                        ui.heard(text + " ")
                        self.log(f"realtime duydu: {text[:160]}")
                        if self.on_heard is not None:
                            try:
                                self.on_heard(text)
                            except Exception:  # noqa: BLE001 - a screen error must not break the conversation
                                pass
                        if self._is_hush(text):
                            # "Sus" (be quiet), "sana demedim" (I wasn't talking to you): cut the playing audio, cancel the answer
                            # being produced and close the session. Before, only the transcript queue was emptied; the server kept
                            # producing the answer and ALICE kept talking.
                            self.hushed = True
                            out.clear()
                            if state["responding"]:
                                send({"type": "response.cancel"})
                            self.log(f"realtime susturuldu: {text[-80:]}")
                            state["done"] = True
                        elif state["gate"] == "pending":
                            if self._addressed(text):
                                state["gate"] = "open"
                                send({"type": "response.create", "response": {"output_modalities": ["audio"]}})
                            else:
                                self.declined = True
                                state["done"] = True
                        touch()
                elif kind == "input_audio_buffer.speech_started":
                    # With echo protection on, this event mostly comes from the previous few mic frames.
                    # Don't clear the answer or extend the speaking time: both cut the sentence off halfway.
                    if self.echo_guard and (self.playing or time.monotonic() < state["output_until"]):
                        if not state["echo_notice"]:
                            self.log("realtime: çalarken algılanan ses yankı korumasıyla yok sayıldı")
                            state["echo_notice"] = True
                        continue
                    if not self.echo_guard and self.playing:
                        # Echo protection off (headset, the phone's echo canceller): the server cancels the answer
                        # but audio that already arrived kept playing; go quiet as soon as the user speaks.
                        out.clear()
                    touch()
                elif kind in ("response.function_call_arguments.done",):
                    args = str(ev.get("arguments") or "{}")
                    state["tools"].append(f"{ev.get('name') or '?'}({args[:120]})")
                    state["calls"][str(ev.get("call_id") or "")] = {
                        "id": str(ev.get("call_id") or ""), "type": "function",
                        "function": {"name": str(ev.get("name") or ""), "arguments": str(ev.get("arguments") or "{}")}}
                elif kind == "response.done":
                    state["responding"] = False
                    status = str((ev.get("response") or {}).get("status") or "")
                    if status and status not in ("completed", "in_progress"):
                        self.log(f"realtime cevap erken bitti: {status}")
                    calls = [c for c in state["calls"].values() if c["function"]["name"]]
                    state["calls"] = {}
                    if calls:
                        if tool_thread is not None and tool_thread.is_alive():
                            tool_thread.join(5)
                        tool_thread = threading.Thread(target=self._handle_calls, args=(calls, send), daemon=True)
                        tool_thread.start()
                    else:
                        if self.single_turn and state["answered"] and status != "cancelled":
                            # response.done says production finished, not playback: exiting here made close() drop
                            # the queued audio and ALICE went quiet mid-sentence. Exit when the audio has finished.
                            state["finish"] = True
                        if not self.playing:
                            touch()
                elif kind == "session.created" or kind == "session.updated":
                    continue
                elif kind == "error":
                    if (ev.get("error") or {}).get("code") == "response_cancel_not_active":
                        continue  # there was no answer to silence (tap/interruption): harmless
                    self.log(f"realtime hata: {json.dumps(ev.get('error') or ev, ensure_ascii=False)[:300]}")
                    info = ev.get("error") or {}
                    err = str(info.get("code") or info.get("type") or "")
                    msg = str(info.get("message") or "")
                    if err in ("invalid_api_key", "session_expired"):
                        raise LiveError(f"realtime oturumu geçersiz: {err}. {msg[:120]}")
                    if any(k in (err + msg).lower() for k in ("rate limit", "quota", "exhausted", "billing", "429")):
                        # Quota/limit reached: instead of retrying on every "Hey Alice", VoiceAssistant
                        # uses the normal voice path for an hour (LiveError.limited tells this from the message).
                        raise LiveError(f"realtime quota/rate limit: {err}. {msg[:160]}")
                    if err in ("invalid_model",):
                        raise LiveError(f"realtime modeli desteklenmiyor: {msg[:160]}")
        finally:
            self.agent.ui = old_ui
            try:
                if not self.declined:  # a sentence said to a friend must not go into the chat history
                    self._remember(f"{first_text} {state['heard']}", " ".join(state["said_parts"] + [state["said"]]),
                                   state["tools"])
            except Exception:  # noqa: BLE001 - logging must not break the conversation
                pass
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            if not self.hushed and not stop.is_set():
                # Even if the connection dropped or the session ended for another reason, play the audio that
                # arrived to the end; close() empties the queue. "Alice sus" and stopping cut it off without waiting.
                drain = getattr(out, "drain", None)
                if callable(drain):
                    try:
                        drain(60.0, stop.is_set)
                    except Exception:  # noqa: BLE001
                        pass
            try:
                out.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws, self._out = None, None
            self._hud("idle", "")
            if self.prewarm and self._connect is None and not self.hushed and not stop.is_set():
                # Follow-up (no "Alice" needed) must not wait for connection + settings: keep the next session ready.
                try:
                    self.prefetch()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ from outside (phone call)
    def inject(self, text: str) -> bool:
        """Send a written message to the open session (\"Yaz\" in a call). False if there is no session."""
        ws = self._ws
        if ws is None or not text.strip():
            return False
        try:
            ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": text}]}}, ensure_ascii=False))
            ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["audio"]}}))
            return True
        except Exception:  # noqa: BLE001
            return False

    def interrupt(self) -> None:
        """Silence ALICE (don't touch the orb during a call): playing audio is cut, the ongoing reply is cancelled."""
        out, ws = self._out, self._ws
        if out is not None:
            out.clear()
        if ws is not None:
            try:
                ws.send(json.dumps({"type": "response.cancel"}))
            except Exception:  # noqa: BLE001 - if no reply is in progress the server returns an error; harmless
                pass

    # ------------------------------------------------------------------ helpers
    def _addressed(self, text: str) -> bool:
        """Was the follow-up sentence addressed to ALICE (Jev)? If it can't be checked, yes: don't lock the conversation."""
        from alice.voice.addressed import is_addressed

        last = self._history[-1][2] if self._history else ""
        ok, p = is_addressed(self.cfg, text, last, log=self.log)
        if p is not None:
            who = "ALICE'e" if ok else "arkadaşlara/kendine, cevap yok"
            self.log(f"hitap kontrolü: {who} (p={p:.2f})")
        return ok

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any]:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            got = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return got if isinstance(got, dict) else {}

    @staticmethod
    def _is_hush(text: str) -> bool:
        """Is this a transcript hush? Sentences like "Sus ya, sus ya." (punctuated and repeated), ones ending with
        "... Alice yeter" (said at the end), and "sana demedim / sana demiyorum" (not said to me) also count.
        Before, words were compared with punctuation: "Sus!"/"Sus." alone did not hush; "sana demedim" was missed."""
        from alice.voice.lang import not_for_me
        from alice.voice.listen import ends_with_hush, is_hush

        low = (text or "").strip()
        if not low:
            return False
        if not_for_me(low):
            return True
        parts = [x for x in re.split(r"[.!?,;:\n]+", low) if x.strip()]
        if any(is_hush(x) or ends_with_hush(x) for x in parts) or ends_with_hush(low):
            return True
        words = re.findall(r"\w+", low.replace("I", "ı").replace("İ", "i").lower())
        return any(w in HUSH_WORDS for w in words)

    def _hud(self, state: str, detail: str) -> None:
        if not self.hud_on:
            return
        try:
            from alice.hud import views

            views.voice_state(state, detail)
        except Exception:  # noqa: BLE001 - don't break the conversation if there is no HUD
            pass

    def _handle_calls(self, calls: list[dict[str, Any]], send: Callable[[dict[str, Any]], None]) -> None:
        """Run the tools, write the results back to the model, and regenerate the reply."""
        outputs = []
        thought = ""
        for call in calls:
            res = self._run_tool(call)
            text = res.text if isinstance(res, ToolResult) else str(res)
            if call.get("function", {}).get("name") == "think" and not getattr(res, "is_error", False):
                thought = text
            outputs.append({"type": "function_call_output", "call_id": call["id"],
                            "output": json.dumps({"ok": not getattr(res, "is_error", False),
                                                  "result": text[:4000]}, ensure_ascii=False)})
        try:
            for item in outputs:
                send({"type": "conversation.item.create", "item": item})
            send({"type": "response.create", "response": {"output_modalities": ["audio"]}})
        except Exception as e:  # noqa: BLE001
            self.log(f"realtime: araç sonucu gönderilemedi: {e}")
            # The session closed in the meantime (connection dropped, user hushed): don't lose think's reply.
            if thought and callable(self.announce):
                try:
                    self.announce(thought[:600])
                except Exception:  # noqa: BLE001
                    pass

    def _run_tool(self, call: dict[str, Any]) -> ToolResult:
        if call.get("function", {}).get("name") == "think":
            return self._think(str(call["function"].get("arguments") or "{}"))
        if self._tools_q is None:
            q: queue.Queue = queue.Queue()
            self._tools_q = q
            threading.Thread(target=self._tool_worker, args=(q,), daemon=True, name="alice-live-rt-tools").start()
        job: dict[str, Any] = {"call": call, "done": threading.Event()}
        self._tools_q.put(job)
        if not job["done"].wait(self.TOOL_TIMEOUT):
            self.log(f"realtime araç takıldı: {call['function']['name']}")
            return ToolResult(f"{call['function']['name']} {self.TOOL_TIMEOUT:.0f} sn içinde bitmedi", is_error=True)
        return job["res"]

    def _think(self, raw: str) -> ToolResult:
        """The fast voice model hands the job to the strong model: the ALICE agent runs the request with its tools.

        The agent's reply (it may be a question too) goes back to the model; the live model tells it to the user.
        Since the same agent object is reused, later `think` calls continue the conversation where it left off."""
        try:
            args = json.loads(raw or "{}")
            request = str(args.get("request") or "").strip()
        except (TypeError, ValueError):
            return ToolResult("think: argüman geçerli JSON değil", is_error=True)
        if not request:
            return ToolResult("think: istek boş", is_error=True)
        if not self._think_lock.acquire(timeout=2):
            return ToolResult("think: önceki istek hâlâ çalışıyor, bitince tekrar sor", is_error=True)
        try:
            text = request
            try:
                from alice.voice.assistant import with_clock

                text = with_clock(request)
            except Exception:  # noqa: BLE001 - if the clock can't be added, the request goes out as is
                pass
            old_role = getattr(self.agent, "role", None)
            role = self._think_role(request)
            t0 = time.monotonic()
            try:
                if role:
                    self.agent.role = role
                reply = str(self.agent.run_turn(text) or "").strip()
            except Exception as e:  # noqa: BLE001 - don't let the live conversation crash
                return ToolResult(f"think hatası: {type(e).__name__}: {e}", is_error=True)
            finally:
                if role:
                    self.agent.role = old_role
                self.log(f"realtime think {time.monotonic() - t0:.1f} sn [{role or old_role}]")
            return ToolResult(reply or "Güçlü model boş cevap döndürdü.", is_error=not reply)
        finally:
            self._think_lock.release()

    THINK_ROLE = "live-think"

    def _think_role(self, request: str = "") -> str | None:
        """Temporary role for think: live_think_models, or if empty, the measured fast voice models + brain chain.
        If Jev routing is on, heavy jobs ([routing] voice_strong, e.g. Sonnet 5.5) are moved to the front."""
        router = getattr(self.agent, "router", None)
        if router is None or not hasattr(router, "role_overrides"):
            return None
        try:
            refs = list(self.think_models) or (router.auto_chain("voice")[:3] + router.chain("brain"))
        except Exception:  # noqa: BLE001 - no measurement/role info: the agent's own chain
            return None
        chain = [r for i, r in enumerate(refs) if r not in refs[:i]]
        if not chain:
            return None
        if request:
            try:
                from alice.providers import jev_route

                chain, r = jev_route.voice_chain(router, self.cfg, request, chain, log=self.log)
                self.log(f"realtime think yönlendirme: {r.tier} ({r.reason}) -> {chain[0]}")
            except Exception as e:  # noqa: BLE001 - if routing fails: the fast chain
                self.log(f"realtime think yönlendirme hatası: {e}")
        router.role_overrides[self.THINK_ROLE] = chain
        return self.THINK_ROLE

    def _tool_worker(self, q: "queue.Queue[dict[str, Any]]") -> None:
        try:
            import comtypes  # so UI Automation also works on this thread

            comtypes.CoInitialize()
        except Exception:  # noqa: BLE001
            pass
        while True:
            job = q.get()
            call = job["call"]
            try:
                self.agent._enable_groups_for(tool_names=(call["function"]["name"],))
                job["res"] = self.agent._execute_calls([call])[0]
            except Exception as e:  # noqa: BLE001
                job["res"] = ToolResult(f"{type(e).__name__}: {e}", is_error=True)
            job["done"].set()
