import json

from alice.computer_agent import ComputerAgent, Screen, Shot, parse_action


class FakeScreen:
    def __init__(self):
        self.acts = []

    def observe(self):
        return Shot("IMG", 0, 0, 1920, 1080, [{"id": 1, "role": "Button", "name": "Kaydet", "center": (100, 50),
                                               "rect": (90, 40, 110, 60)}], "Not Defteri")

    def act(self, a, shot):
        self.acts.append(a)
        return "ok"


def test_agent_loops_until_done_and_sends_screenshot():
    answers = iter(['{"action": "click", "id": 1, "thought": "kaydet"}',
                    'Tamam: {"action": "done", "summary": "Kaydedildi."}'])
    seen = []

    def ask(messages):
        seen.append(messages)
        return next(answers)

    s = FakeScreen()
    res = ComputerAgent(s, ask, settle=0).run("dosyayı kaydet")
    assert res.status == "done" and res.summary == "Kaydedildi."
    assert s.acts == [{"action": "click", "id": 1, "thought": "kaydet"}]
    parts = seen[0][1]["content"]
    assert "[1] Button 'Kaydet'" in parts[0]["text"] and parts[1]["image_url"]["url"].endswith("IMG")
    assert "1. [{\"action\": \"click\"" in seen[1][1]["content"][0]["text"]  # past steps carry over


def test_agent_stops_on_repeated_action():
    """Same step three times: ask for a different approach once; if it is still the same, give up."""
    s = FakeScreen()
    seen = []

    def ask(m):
        seen.append(m[1]["content"][0]["text"])
        return '{"action": "key", "keys": "enter"}'

    res = ComputerAgent(s, ask, settle=0).run("x")
    assert res.status == "fail" and len(s.acts) == 4
    assert any("repeated the same action 3 times" in t for t in seen)  # recovery warning reached the model
    assert any("did not change the screen" in t for t in seen)  # model knew the screen never changed


class ChangingScreen(FakeScreen):
    def __init__(self):
        super().__init__()
        self.n = 0

    def observe(self):
        self.n += 1
        shot = super().observe()
        shot.png_b64 = f"IMG{self.n}"  # the screen changes each step: work is moving forward
        return shot


def test_agent_extends_step_limit_once_when_screen_keeps_changing(tmp_path):
    s = ChangingScreen()
    n = iter(range(100))
    res = ComputerAgent(s, lambda m: json.dumps({"action": "scroll", "amount": next(n)}), settle=0).run(
        "x", max_steps=5)
    assert res.status == "max_steps" and len(s.acts) == 5 + ComputerAgent.EXTEND_STEPS
    from alice.computer_agent import load_recipes

    assert load_recipes() and load_recipes()[-1]["status"] == "fail"  # unfinished work is saved as a lesson too


def test_point_mapping_uses_ids_and_normalized_coords():
    shot = Shot("", 100, 0, 2000, 1000, [{"id": 3, "center": (500, 400)}])
    assert Screen.point({"id": 3}, shot) == (500, 400)
    assert Screen.point({"point": [500, 250]}, shot) == (600, 500)  # [y, x] 0-1000
    assert parse_action('```json\n{"action": "wait"}\n```') == {"action": "wait"}


def test_live_runs_computer_task_in_background(monkeypatch):
    import threading
    from types import SimpleNamespace

    from alice.tools.base import ToolResult
    from alice.voice.live import LiveSession

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    sent = []
    done = threading.Event()

    class WS:
        def send(self, data):
            sent.append(json.loads(data))
            if "clientContent" in data:
                done.set()

    def execute(calls):
        return [ToolResult("done: ev yapıldı")]

    agent = SimpleNamespace(_enable_groups_for=lambda **k: None, _execute_calls=execute)
    s = LiveSession(SimpleNamespace(data={"voice": {}}), agent)
    s._run_call(WS(), {"id": "c1", "name": "computer_task", "args": {"goal": "ev yap"}})
    assert "başladı" in sent[0]["toolResponse"]["functionResponses"][0]["response"]["result"]
    assert done.wait(3)
    assert "ev yapıldı" in sent[-1]["clientContent"]["turns"][0]["parts"][0]["text"]
    assert s._bg == 0


def test_agent_runs_batched_actions():
    answers = iter(['{"thought": "kutu görünüyor", "actions": [{"action": "click", "id": 1}, '
                    '{"action": "type", "text": "merhaba", "enter": true}]}',
                    '{"action": "done", "summary": "Gönderildi."}'])
    s = FakeScreen()
    res = ComputerAgent(s, lambda m: next(answers), settle=0).run("merhaba yaz")
    assert res.status == "done" and [a["action"] for a in s.acts] == ["click", "type"]


def test_agent_stops_when_vision_model_unavailable():
    def ask(messages):
        raise RuntimeError("429 You exceeded your current quota")

    s = FakeScreen()
    res = ComputerAgent(s, ask, settle=0).run("x")
    assert res.status == "error" and "quota" in res.summary and s.acts == []


def test_parse_action_nested():
    assert parse_action('{"action": {"point": [10, 20]}}') == {"action": "click", "point": [10, 20]}


def test_live_refuses_second_screen_task_while_one_runs(monkeypatch):
    import threading
    from types import SimpleNamespace

    from alice.tools.base import ToolResult
    from alice.voice.live import LiveSession

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    release = threading.Event()
    runs = []

    def execute(calls):
        runs.append(calls)
        release.wait(3)
        return [ToolResult("done")]

    sent = []
    s = LiveSession(SimpleNamespace(data={"voice": {}}),
                    SimpleNamespace(_enable_groups_for=lambda **k: None, _execute_calls=execute))
    ws = SimpleNamespace(send=lambda d: sent.append(json.loads(d)))
    s._run_call(ws, {"id": "1", "name": "computer_task", "args": {"goal": "a"}})
    s._run_call(ws, {"id": "2", "name": "computer_task", "args": {"goal": "a"}})
    release.set()
    results = [m["toolResponse"]["functionResponses"][0]["response"]["result"] for m in sent if "toolResponse" in m]
    assert "başladı" in results[0] and "zaten" in results[1]


def test_agent_remembers_what_worked_for_similar_goals():
    from alice.computer_agent import load_recipes

    answers = iter(['{"action": "click", "id": 1, "thought": "kaydet"}', '{"action": "done", "summary": "Kaydedildi."}',
                    '{"action": "done", "summary": "Yine kaydedildi."}'])
    seen = []

    def ask(messages):
        seen.append(messages[1]["content"][0]["text"])
        return next(answers)

    assert ComputerAgent(FakeScreen(), ask, settle=0).run("not defterindeki dosyayı kaydet").status == "done"
    assert "PAST SUCCESS" not in seen[0]
    assert load_recipes()[0]["status"] == "done"
    ComputerAgent(FakeScreen(), ask, settle=0).run("not defterinde dosyayı kaydet")
    assert "PAST SUCCESS" in seen[2]
    assert '"click"' in seen[2]  # last time's step came back as a hint


def test_failed_attempt_is_a_warning_next_time():
    seen = []
    s = FakeScreen()
    ComputerAgent(s, lambda m: '{"action": "key", "keys": "enter"}', settle=0).run("ayarlardan bluetooth aç")

    def ask(messages):
        seen.append(messages[1]["content"][0]["text"])
        return '{"action": "done", "summary": "açıldı"}'

    ComputerAgent(FakeScreen(), ask, settle=0).run("ayarlardan bluetooth aç")
    assert "PAST FAILED ATTEMPT" in seen[0] and "keeps repeating" in seen[0]
    assert ComputerAgent(FakeScreen(), ask, settle=0, learn=False).run("ayarlardan bluetooth aç").status == "done"
