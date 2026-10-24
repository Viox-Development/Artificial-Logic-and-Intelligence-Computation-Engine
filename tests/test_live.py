from __future__ import annotations

import base64
import json
import queue
import threading
import time
from types import SimpleNamespace

from alice.tools.base import ToolResult
from alice.voice.live import LiveSession, declarations, sanitize_schema


import pytest


@pytest.fixture(autouse=True)
def no_screen(monkeypatch):
    monkeypatch.setattr("alice.voice.live.screen_frame", lambda: None)  # testler gerçek ekranı çekmesin


def test_sanitize_schema_for_gemini():
    s = {"type": "object", "additionalProperties": False, "properties": {
        "a": {"type": ["string", "null"], "default": "x"}, "b": {"type": "array", "items": {"type": "integer"}}},
        "required": ["a"]}
    assert sanitize_schema(s) == {"type": "OBJECT", "properties": {
        "a": {"type": "STRING", "nullable": True}, "b": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
        "required": ["a"]}
    d = declarations([{"type": "function", "function": {"name": "t", "description": "d",
                                                         "parameters": {"type": "object", "properties": {}}}}])
    assert d == [{"name": "t", "description": "d"}]


class FakeWS:
    def __init__(self, script):
        self.sent: list[dict] = []
        self.inbox: queue.Queue = queue.Queue()
        self.script = script
        self.closed = False

    def send(self, data):
        msg = json.loads(data)
        self.sent.append(msg)
        if "setup" in msg:
            self.inbox.put({"setupComplete": {}})
        elif "clientContent" in msg:
            for m in self.script:
                self.inbox.put(m)
        elif "toolResponse" in msg:
            self.inbox.put({"serverContent": {"outputTranscription": {"text": "YouTube açıldı."},
                                              "turnComplete": True}})

    def recv(self, timeout=None):
        while not self.closed:
            try:
                return json.dumps(self.inbox.get(timeout=0.1))
            except queue.Empty:
                continue
        raise ConnectionError("closed")

    def close(self):
        self.closed = True


class FakeOut:
    def __init__(self):
        self.chunks = []

    def put(self, pcm):
        self.chunks.append(pcm)

    def clear(self):
        pass

    playing = False

    def close(self):
        pass


def test_live_converse_runs_tools_and_plays_audio(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    pcm = base64.b64encode(b"\x01\x00" * 100).decode()
    ws = FakeWS([
        {"serverContent": {"inputTranscription": {"text": "YouTube'u aç"},
                           "modelTurn": {"parts": [{"inlineData": {"data": pcm}}]}}},
        {"toolCall": {"functionCalls": [{"id": "c1", "name": "windows",
                                         "args": {"action": "open", "target": "https://youtube.com"}}]}},
    ])
    ran = []
    agent = SimpleNamespace(
        messages=[{"role": "system", "content": "sys"}], ctx=SimpleNamespace(state={}), ui=None,
        tool_specs=lambda: [{"type": "function", "function": {"name": "windows", "description": "w",
                                                               "parameters": {"type": "object", "properties": {
                                                                   "action": {"type": "string"}}}}}],
        _enable_groups_for=lambda **k: None,
        _execute_calls=lambda calls: ran.append(calls) or [ToolResult("açıldı")])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.6, "live_models": ["m1"]}})
    out = FakeOut()
    logs = []
    s = LiveSession(cfg, agent, log=logs.append, connect=lambda url, **k: ws, out_factory=lambda: out)
    assert s.available
    t0 = time.monotonic()
    s.converse("YouTube'u aç", queue.Queue(), threading.Event())
    assert time.monotonic() - t0 < 5
    setup = ws.sent[0]["setup"]
    assert setup["model"] == "models/m1" and setup["tools"][0]["functionDeclarations"][0]["name"] == "windows"
    assert json.loads(ran[0][0]["function"]["arguments"]) == {"action": "open", "target": "https://youtube.com"}
    resp = next(m for m in ws.sent if "toolResponse" in m)["toolResponse"]["functionResponses"][0]
    assert resp == {"id": "c1", "name": "windows", "response": {"result": "açıldı"}}
    assert out.chunks == [b"\x01\x00" * 100]
    assert any("YouTube açıldı." in line for line in logs)
    assert agent.ui is None  # eski UI geri kondu


def test_prefetch_reuses_connection_and_discard_closes(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    opened = []

    def connect(url, **k):
        ws = FakeWS([])
        opened.append(ws)
        return ws

    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.3, "live_models": ["m1"]}})
    s = LiveSession(cfg, agent, connect=connect, out_factory=FakeOut)
    s.prefetch()
    s.discard()
    assert _wait(lambda: len(opened) == 1 and opened[0].closed)
    s.prefetch()
    s.converse("", queue.Queue(), threading.Event(), first_audio=b"\0\0" * 1600)
    assert len(opened) == 2  # converse önceden açılan bağlantıyı kullandı
    audio = [m for m in opened[1].sent if "realtimeInput" in m]
    assert len(audio) == 2  # cümle + arkasından sessizlik


def _wait(cond, t=3):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_looks_internal_catches_leaked_notes_only():
    from alice.voice.live import looks_internal

    assert looks_internal("TIRED: I made a syntax error in the function call")
    assert looks_internal("FIXED: Call `browser` properly")
    assert not looks_internal("Tamam, YouTube'u açtım. Saat on üçü çeyrek geçiyor.")
    assert not looks_internal("Alice burada, nasıl yardımcı olayım?")


def test_live_mutes_leaked_internal_note(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    good = base64.b64encode(b"\x01\x00" * 10).decode()
    bad = base64.b64encode(b"\x02\x00" * 10).decode()
    ws = FakeWS([
        {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": good}, "thought": True}]}}},
        {"serverContent": {"outputTranscription": {"text": "TIRED: I made a syntax error"}}},
        {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": bad}}]}}},
        {"serverContent": {"turnComplete": True}},
        {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": good}}]}}},
    ])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "sys"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.6, "live_models": ["m1"]}})
    out = FakeOut()
    logs = []
    s = LiveSession(cfg, agent, log=logs.append, connect=lambda url, **k: ws, out_factory=lambda: out)
    s.converse("selam", queue.Queue(), threading.Event())
    assert out.chunks == [b"\x01\x00" * 10]  # düşünce ve iç not çalınmadı, sonraki tur çalındı
    assert any("iç not susturuldu" in line for line in logs)


def test_live_tool_timeout_answers_model(monkeypatch):
    sent = []
    ws = SimpleNamespace(send=lambda d: sent.append(json.loads(d)))
    agent = SimpleNamespace(_enable_groups_for=lambda **k: None,
                            _execute_calls=lambda c: time.sleep(0.3) or [ToolResult("geç sonuç")])
    s = LiveSession.__new__(LiveSession)
    s.agent, s.log = agent, lambda m: None
    s.TOOL_TIMEOUT = 0.05
    s._run_call(ws, {"id": "c1", "name": "browser", "args": {"action": "snapshot"}})
    assert len(sent) == 1  # geç gelen sonuç ikinci kez gönderilmez
    assert "bitmedi" in sent[0]["toolResponse"]["functionResponses"][0]["response"]["result"]


def test_live_first_turn_uses_local_text_and_names(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ws = FakeWS([{"serverContent": {"turnComplete": True}}])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.3, "live_models": ["m1"]}})
    s = LiveSession(cfg, agent, connect=lambda url, **k: ws, out_factory=FakeOut)
    s.names = lambda: ["GitHub", "YouTube Music"]
    s.converse("", queue.Queue(), threading.Event(), first_audio=b"\0\0" * 1600, hint=lambda: "GitHub'ı açar mısın")
    assert "GitHub, YouTube Music" in ws.sent[0]["setup"]["systemInstruction"]["parts"][0]["text"]
    assert not [m for m in ws.sent if "realtimeInput" in m]  # ses yerine yazı gitti
    turn = next(m for m in ws.sent if "clientContent" in m)["clientContent"]["turns"][0]
    assert turn["parts"][0]["text"] == "GitHub'ı açar mısın"


def test_live_first_turn_falls_back_to_audio_without_text(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ws = FakeWS([])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.3, "live_models": ["m1"]}})
    s = LiveSession(cfg, agent, connect=lambda url, **k: ws, out_factory=FakeOut)
    s.names = lambda: []
    s.converse("", queue.Queue(), threading.Event(), first_audio=b"\0\0" * 1600, hint=lambda: None)
    assert len([m for m in ws.sent if "realtimeInput" in m]) == 2
    assert not [m for m in ws.sent if "clientContent" in m]


def test_live_sends_screen_frame_each_turn(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ws = FakeWS([{"serverContent": {"inputTranscription": {"text": "ekrandaki"}}},
                 {"serverContent": {"turnComplete": True}},
                 {"serverContent": {"inputTranscription": {"text": "şunu aç"}}}])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.5, "live_models": ["m1"], "live_screen": "turn"}})
    s = LiveSession(cfg, agent, connect=lambda url, **k: ws, out_factory=FakeOut)
    s.names = lambda: []
    s.grab = lambda: "IMG"
    s.converse("ekrandakini aç", queue.Queue(), threading.Event())
    first = next(m for m in ws.sent if "clientContent" in m)["clientContent"]["turns"][0]["parts"]
    assert first[0] == {"inlineData": {"mimeType": "image/jpeg", "data": "IMG"}}
    frames = [m for m in ws.sent if "video" in m.get("realtimeInput", {})]
    assert len(frames) == 1  # ilk tur yazıyla birlikte gitti; ikinci tur başında bir kare daha


def test_live_screen_off_by_default(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    cfg = SimpleNamespace(data={"voice": {}})
    assert LiveSession(cfg, SimpleNamespace()).screen == "off"


def test_reflex_works_in_users_visible_tab(monkeypatch):
    from alice.reflex import observers
    from alice.tools import browser

    calls = []
    visible = SimpleNamespace(url="https://music.youtube.com/")
    fake = SimpleNamespace(attached=True, page=SimpleNamespace(url="about:blank"), _visible_page=lambda: visible)
    monkeypatch.setattr(browser, "get_browser", lambda ctx, separate=False: calls.append(separate) or fake)
    ob = observers.BrowserObserver(SimpleNamespace())
    assert calls == [False]  # ayrı pencere istemedi
    assert ob.b.page is visible


def test_live_declares_all_builtin_tools_upfront(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")

    def tool(name):
        return SimpleNamespace(spec=lambda: {"type": "function", "function": {"name": name, "description": name}})

    agent = SimpleNamespace(tools={n: tool(n) for n in ("blender", "computer", "load_tools", "mcp__github__x")},
                            tool_specs=lambda: [])
    s = LiveSession(SimpleNamespace(data={"voice": {}}), agent)
    assert [d["function"]["name"] for d in s._specs()] == ["blender", "computer"]


def test_stuck_tool_does_not_lock_conversation(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    release = threading.Event()
    sent = []

    def execute(calls):
        if calls[0]["function"]["name"] == "ui_elements":
            release.wait(5)  # takılan araç
        return [ToolResult("ok")]

    state = {"browser": object()}
    agent = SimpleNamespace(_enable_groups_for=lambda **k: None, _execute_calls=execute,
                            ctx=SimpleNamespace(state=state))
    s = LiveSession(SimpleNamespace(data={"voice": {}}), agent)
    s.TOOL_TIMEOUT = 0.2
    ws = SimpleNamespace(send=lambda d: sent.append(json.loads(d)))
    t0 = time.monotonic()
    s._run_call(ws, {"id": "1", "name": "ui_elements", "args": {}})
    assert time.monotonic() - t0 < 2 and "browser" not in state
    s._run_call(ws, {"id": "2", "name": "windows", "args": {}})  # yeni iş parçacığında hemen çalışır
    results = [m["toolResponse"]["functionResponses"][0]["response"]["result"] for m in sent]
    assert "YAPILMADI" in results[0] and results[1] == "ok"
    release.set()


def _guard_session(budget=3):
    sent = []
    agent = SimpleNamespace(_enable_groups_for=lambda **k: None,
                            _execute_calls=lambda c: [ToolResult("sonuç " + c[0]["function"]["arguments"])])
    s = LiveSession(SimpleNamespace(data={"voice": {"live_tool_budget": budget}}), agent)
    s.TOOL_TIMEOUT = 2
    ws = SimpleNamespace(send=lambda d: sent.append(json.loads(d)))
    results = lambda: [m["toolResponse"]["functionResponses"][0]["response"]["result"] for m in sent]  # noqa: E731
    return s, ws, results


def test_live_tool_budget_stops_runaway_loop():
    s, ws, results = _guard_session(budget=3)
    for i in range(5):
        s._run_call(ws, {"id": str(i), "name": "run_command", "args": {"command": f"komut {i}"}})
    r = results()
    assert r[:3] == [f'sonuç {{"command": "komut {i}"}}' for i in range(3)]
    assert all("DUR" in x and "ÇALIŞTIRILMADI" in x for x in r[3:])
    s._new_turn()  # kullanıcı tekrar konuştu: sayaç sıfırlanır
    s._run_call(ws, {"id": "9", "name": "run_command", "args": {"command": "yeni"}})
    assert results()[-1].startswith("sonuç")


def test_live_consecutive_duplicate_call_not_rerun():
    s, ws, results = _guard_session()
    calls = []
    s.agent._execute_calls = lambda c: calls.append(c) or [ToolResult("adım 3")]
    s._run_call(ws, {"id": "1", "name": "flight", "args": {"action": "checklist_next"}})
    s._run_call(ws, {"id": "2", "name": "flight", "args": {"action": "checklist_next"}})
    assert len(calls) == 1 and "tekrar çalıştırılmadı" in results()[1] and "adım 3" in results()[1]
    s._run_call(ws, {"id": "3", "name": "computer", "args": {"action": "scroll"}})
    s._run_call(ws, {"id": "4", "name": "computer", "args": {"action": "scroll"}})
    assert len(calls) == 3  # kaydırma gibi tekrar edilebilir işler engellenmez


def test_live_idle_is_capped_and_hush_closes_session(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ws = FakeWS([{"serverContent": {"inputTranscription": {"text": "Alice "}}},
                 {"serverContent": {"inputTranscription": {"text": "sus"}}}])
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])
    assert LiveSession(SimpleNamespace(data={"voice": {"live_idle": 30}}), agent, connect=lambda u, **k: ws).idle == 5
    cfg = SimpleNamespace(data={"voice": {"live_idle": 3, "live_models": ["m1"]}})
    s = LiveSession(cfg, agent, connect=lambda url, **k: ws, out_factory=FakeOut)
    t0 = time.monotonic()
    s.converse("selam", queue.Queue(), threading.Event())
    assert time.monotonic() - t0 < 2 and s.hushed and s.last_active > 0


def _session(ws_for, **voice):
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [])
    cfg = SimpleNamespace(data={"voice": {"live_models": ["m1"], **voice}})
    logs: list[str] = []
    return LiveSession(cfg, agent, log=logs.append, connect=ws_for), logs


def test_live_asks_for_affective_dialog_and_proactive_audio(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    urls, sockets = [], []

    def connect(url, **k):
        urls.append(url)
        sockets.append(FakeWS([]))
        return sockets[-1]

    s0, _ = _session(connect)
    s0._open()  # varsayılan kapalı: desteklemeyen modelde ilk seste 1007 ile kopuyordu
    assert "v1beta" in urls[0] and "proactivity" not in sockets[0].sent[0]["setup"]
    urls.clear()
    sockets.clear()
    s, _ = _session(connect, live_affective=True, live_proactive=True)
    s._open()
    setup = sockets[0].sent[0]["setup"]
    assert "v1alpha" in urls[0] and setup["generationConfig"]["enableAffectiveDialog"] is True
    assert setup["proactivity"] == {"proactiveAudio": True}
    s2, _ = _session(connect, live_affective=False, live_proactive=False)
    s2._open()
    plain = sockets[-1].sent[0]["setup"]
    assert "v1beta" in urls[-1] and "proactivity" not in plain and "enableAffectiveDialog" not in plain["generationConfig"]


def test_live_falls_back_without_extras_when_model_rejects_them(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    urls = []

    class Picky(FakeWS):
        def send(self, data):
            msg = json.loads(data)
            if "setup" in msg and "proactivity" in msg["setup"]:
                self.sent.append(msg)
                self.inbox.put({"error": {"message": "Unknown name proactivity"}})
                return
            super().send(data)

    def connect(url, **k):
        urls.append(url)
        return Picky([])

    s, logs = _session(connect, live_affective=True, live_proactive=True)
    s._open()
    assert s.model == "m1" and "v1alpha" in urls[0] and "v1beta" in urls[1]
    assert any("desteklenmiyor" in line for line in logs)
    urls.clear()
    s._open()  # bir kez reddeden modelde bir daha denenmez
    assert len(urls) == 1 and "v1beta" in urls[0]


def test_canned_replies_are_kept_out_of_history():
    from alice.voice.live import canned_reply

    for r in ("Buradayım efendim, size nasıl yardımcı olabilirim?", "Anladım efendim, buradayım. Başka bir konuda "
              "yardımcı olabilir miyim?", "Anlayamadım, tekrar eder misiniz?", "Rica ederim, her zaman buradayım."):
        assert canned_reply(r), r
    assert not canned_reply("Hızın 367 knot, irtifa 1100 metre.")
    s, _ = _session(lambda url, **k: FakeWS([]))
    now = time.time()
    s._history = [(now, "Aferin.", "Rica ederim, her zaman buradayım. Başka bir konuda yardımcı olabilir miyim?"),
                  (now, "hızım kaç", "367 knot.")]
    s._memory = lambda: None
    text = s.history_text()
    assert "Aferin." in text and "Rica ederim" not in text and "367 knot." in text


def test_live_same_read_is_not_rerun_even_with_calls_between():
    # voice.log (10 Ekim): Get-Volume ve İndirilenler listesi aynı istekte arada başka komutlarla 5-6 kez çalıştı.
    s, ws, results = _guard_session(budget=20)
    calls = []
    s.agent._execute_calls = lambda c: calls.append(c) or [ToolResult("C: D:")]
    vol = {"id": "1", "name": "run_command", "args": {"command": "Get-Volume"}}
    s._run_call(ws, vol)
    s._run_call(ws, {"id": "2", "name": "list_dir", "args": {"path": "İndirilenler"}})
    s._run_call(ws, dict(vol, id="3"))
    assert len(calls) == 2 and "zaten yaptın" in results()[2] and "C: D:" in results()[2]
    # komut çalıştırmak tekrar edilebilir (testi düzelttikten sonra yeniden çalıştırmak gibi)
    build = {"id": "4", "name": "run_command", "args": {"command": "npm test"}}
    s._run_call(ws, build)
    s._run_call(ws, {"id": "5", "name": "list_dir", "args": {"path": "."}})
    s._run_call(ws, dict(build, id="6"))
    assert len(calls) == 5
    s._new_turn()
    s._run_call(ws, dict(vol, id="7"))  # yeni istek: tekrar okunabilir
    assert len(calls) == 6


def test_read_only_detection():
    from alice.voice.live import read_only

    assert read_only("run_command", {"command": "Get-ChildItem C:\\Users\\x\\Downloads"})
    assert read_only("run_command", {"command": "dir C:\\"}) and read_only("list_dir", {"path": "."})
    assert not read_only("run_command", {"command": "Remove-Item x"}) and not read_only("browser", {"action": "click"})


def test_live_single_turn_closes_right_after_the_answer(monkeypatch):
    # Seçici mod (Discord): cevap bitince oturum kapanır; arkadaşlara söylenenler Live'a gitmez.
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    pcm = base64.b64encode(b"\x01\x00" * 100).decode()
    agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}], ctx=SimpleNamespace(state={}), ui=None,
                            tool_specs=lambda: [], _enable_groups_for=lambda **k: None, _execute_calls=lambda c: [])

    def run(single):
        ws = FakeWS([{"serverContent": {"inputTranscription": {"text": "saat kaç"},
                                        "modelTurn": {"parts": [{"inlineData": {"data": pcm}}]},
                                        "outputTranscription": {"text": "Saat beş."}, "turnComplete": True}}])
        s = LiveSession(SimpleNamespace(data={"voice": {"live_idle": 5, "live_models": ["m1"]}}), agent,
                        connect=lambda url, **k: ws, out_factory=FakeOut)
        s.single_turn = single
        t0 = time.monotonic()
        s.converse("saat kaç", queue.Queue(), threading.Event())
        return time.monotonic() - t0

    assert run(True) < 3.0 < run(False)


def test_think_runs_one_at_a_time_and_drops_stale_answers(monkeypatch):
    from alice.voice import live as live_mod

    s, ws, results = _guard_session()
    s._bg = 0
    gate = threading.Event()
    s.think_answer = lambda q, c="": gate.wait(5) and "cevap"
    s.think_timeout = 60
    s._run_think(ws, {"id": "1", "name": "think", "args": {"question": "D&D nedir"}})
    s._run_think(ws, {"id": "2", "name": "think", "args": {"question": "başka soru"}})
    assert "hâlâ düşünülüyor" in results()[0]  # ikinci think çalışmadı, model kendisi cevaplasın
    gate.set()
    deadline = time.monotonic() + 3
    while len(results()) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "cevap" in results()[1] and s._thinking == 0
    # geç cevap: zaman aşımı mesajı gitti, cevap THINK_STALE'den sonra geldi -> söylenmez
    sent = []
    late_ws = SimpleNamespace(send=lambda d: sent.append(json.loads(d)))
    monkeypatch.setattr(live_mod, "THINK_STALE", 0.0)
    s.think_timeout = 0.01
    gate.clear()
    s._run_think(late_ws, {"id": "3", "name": "think", "args": {"question": "yavaş soru"}})
    time.sleep(0.2)
    gate.set()
    deadline = time.monotonic() + 3
    while s._thinking and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [m for m in sent if "toolResponse" in m]  # "uzun sürüyor" cevabı gitti
    assert not [m for m in sent if "clientContent" in m]  # dakikalar sonra gelen cevap okunmadı
