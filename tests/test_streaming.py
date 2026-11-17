from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from alice.agent import Agent, NullUI
from alice.config import ProviderConfig
from alice.permissions import Permissions
from alice.providers.openai_compat import OpenAICompatProvider, ProviderError, StreamCancelled
from alice.providers.router import Router
from alice.tools import ToolContext, all_tools
from alice.tools.files import EditFile, WriteFile
from tests.conftest import FakeProvider, make_config, tool_call


def sse(*chunks) -> bytes:
    out = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return out.encode()


def provider(handler) -> OpenAICompatProvider:
    return OpenAICompatProvider(ProviderConfig("x", "http://x"), client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_stream_text_and_tool_calls_merged():
    body = sse(
        {"model": "m1", "choices": [{"delta": {"content": "Mer"}}]},
        {"choices": [{"delta": {"content": "haba"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "read_file", "arguments": "{\"pa"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\": \"a.txt\"}"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "t2", "function": {"name": "list_dir", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]},
    )
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    deltas = []
    res = provider(handler).chat_stream("m", [{"role": "user", "content": "x"}], deltas.append)
    assert seen["body"]["stream"] is True
    assert deltas == ["Mer", "haba"] and res.text == "Merhaba" and res.model == "m1"
    assert [c["function"]["name"] for c in res.tool_calls] == ["read_file", "list_dir"]
    assert json.loads(res.tool_calls[0]["function"]["arguments"]) == {"path": "a.txt"}


def test_stream_gemini_style_without_index():
    body = sse({"choices": [{"delta": {"tool_calls": [{"id": "g1", "function": {"name": "a", "arguments": "{}"}},
                                                       {"id": "g2", "function": {"name": "b", "arguments": "{}"}}]}}]})
    res = provider(lambda r: httpx.Response(200, content=body)).chat_stream("m", [], lambda t: None)
    assert [c["id"] for c in res.tool_calls] == ["g1", "g2"]


def test_stream_http_error_and_inline_error():
    with pytest.raises(ProviderError) as ei:
        provider(lambda r: httpx.Response(429, text="rate limit, try again")).chat_stream("m", [], lambda t: None)
    assert ei.value.kind == "rate_limit"
    body = sse({"error": {"code": 429, "message": "quota exceeded per day"}})
    with pytest.raises(ProviderError) as ei:
        provider(lambda r: httpx.Response(200, content=body)).chat_stream("m", [], lambda t: None)
    assert ei.value.kind == "quota"


def test_stream_cancel():
    body = sse({"choices": [{"delta": {"content": "a"}}]}, {"choices": [{"delta": {"content": "b"}}]})
    with pytest.raises(StreamCancelled):
        provider(lambda r: httpx.Response(200, content=body)).chat_stream("m", [], lambda t: None, cancel=lambda: True)


class StreamingFake(FakeProvider):
    def chat_stream(self, model, messages, on_delta, cancel=None, **kw):
        item = self.script.pop(0)
        if isinstance(item, tuple):  # (chunk, then error)
            on_delta(item[0])
            raise item[1]
        res = self.chat_from(item, model)
        for i in range(0, len(res.text), 3):
            on_delta(res.text[i:i + 3])
        return res

    def chat_from(self, item, model):
        self.script.insert(0, item)
        return self.chat(model, [])


class Sink:
    def __init__(self):
        self.text = ""
        self.resets = 0

    def on_delta(self, t):
        self.text += t

    def on_reset(self):
        self.text = ""
        self.resets += 1

    def cancelled(self):
        return False


def test_router_stream_resets_and_falls_back_mid_stream():
    cfg = make_config({"brain": ["a:m", "b:m"]})
    a = StreamingFake("a", [("yarım cev", ProviderError("server", "boom", 500))])
    b = StreamingFake("b", ["tam cevap"])
    r = Router(cfg, provider_factory=lambda pc: {"a": a, "b": b}[pc.name], sleep=lambda s: None)
    sink = Sink()
    res = r.chat("brain", [{"role": "user", "content": "x"}], stream=sink)
    assert res.text == "tam cevap" and sink.text == "tam cevap" and sink.resets == 1


def test_agent_streams_and_stop_from_other_thread(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    gate = threading.Event()

    class Slow(StreamingFake):
        def chat_stream(self, model, messages, on_delta, cancel=None, **kw):
            on_delta("başlıyorum")
            gate.wait(5)
            if cancel and cancel():
                raise __import__("alice.providers.openai_compat", fromlist=["x"]).StreamCancelled()
            return super().chat_stream(model, messages, on_delta, cancel, **kw)

    prov = Slow("a", ["bitti"])
    router = Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None)
    ui = NullUI()
    agent = Agent(cfg, router, all_tools(), ToolContext(cwd=workdir, config=cfg, router=router), Permissions("auto"), ui,
                  system_prompt="s")
    t = threading.Thread(target=agent.run_turn, args=("uzun iş",))
    t.start()
    for _ in range(200):  # if stopped before the turn starts, run_turn resets the flag; stop once streaming starts
        if ("delta", "başlıyorum") in ui.events:
            break
        time.sleep(0.01)
    agent.stop()
    gate.set()
    t.join(5)
    assert not agent.busy
    assert ("delta", "başlıyorum") in ui.events
    assert agent.messages[-1]["content"].endswith("[durduruldu]")
    assert ("info", "durduruldu") in ui.events


def test_agent_stream_full_turn(workdir):
    cfg = make_config({"brain": ["a:m"]})
    prov = StreamingFake("a", [tool_call("list_dir", {}), "klasör boş değil"])
    router = Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None)
    ui = NullUI()
    agent = Agent(cfg, router, all_tools(), ToolContext(cwd=workdir, config=cfg, router=router), Permissions("auto"), ui,
                  system_prompt="s")
    assert agent.run_turn("bak") == "klasör boş değil"
    deltas = "".join(e[1] for e in ui.events if e[0] == "delta")
    assert deltas == "klasör boş değil"


def test_diff_on_edit_and_write(workdir):
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    (workdir / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    from alice.tools.files import ReadFile

    ReadFile().run(ctx, path="a.py")  # editing without reading first is rejected
    r = EditFile().run(ctx, path="a.py", old_string="y = 2", new_string="y = 3")
    assert "-y = 2" in r.diff and "+y = 3" in r.diff
    r = WriteFile().run(ctx, path="new.py", content="print(1)\n")
    assert "+print(1)" in r.diff


def test_think_tags_are_hidden_from_text_and_speech():
    from alice.providers.openai_compat import ThinkFilter, normalize_message

    f, shown, thought = ThinkFilter(), "", ""
    for c in ["<thi", "nk>kullanıcı saati soruyor", "</th", "ink>\n\nSaat on bir", " elli."]:
        a, b = f.feed(c)
        shown, thought = shown + a, thought + b
    assert shown == "Saat on bir elli." and thought == "kullanıcı saati soruyor"
    f = ThinkFilter()
    assert f.feed("Merhaba <think> bu metin </think>")[0] == "Merhaba <think> bu metin </think>"
    assert normalize_message({"content": "<think>plan</think>Cevap"})["content"] == "Cevap"
    assert normalize_message({"content": "plan</think>Cevap"})["content"] == "Cevap"


def test_stream_reports_tool_progress():
    body = sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "write_file", "arguments": "{\"pa"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\": \"a.txt\"}"}}]}}]},
    )
    seen = []
    provider(lambda r: httpx.Response(200, content=body)).chat_stream("m", [], lambda t: None,
                                                                      on_tool=lambda n, k: seen.append((n, k)))
    assert seen == [("write_file", 4), ("write_file", 17)]


def test_stream_aborts_when_only_keepalives_arrive():
    class Trickle(httpx.SyncByteStream):  # the provider says "işliyorum" (processing) but never answers
        def __iter__(self):
            for _ in range(200):
                time.sleep(0.01)
                yield b": OPENROUTER PROCESSING\n\n"

    p = provider(lambda r: httpx.Response(200, stream=Trickle()))
    p.STREAM_IDLE = 0.2
    t0 = time.monotonic()
    with pytest.raises(ProviderError) as ei:
        p.chat_stream("m", [], lambda t: None)
    assert ei.value.kind == "network" and "veri göndermiyor" in str(ei.value) and time.monotonic() - t0 < 1.5


def test_stream_gives_up_when_first_answer_never_comes():
    """glm-5.3 (NVIDIA) kept the connection open and sent no answer for over 5 minutes: if the first data never
    comes, give up, with the error 'zaman aşımı' (timeout) so the router skips the model for a while."""
    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(200):
                time.sleep(0.01)
                yield b": OPENROUTER PROCESSING\n\n"

    p = provider(lambda r: httpx.Response(200, stream=Trickle()))
    p.FIRST_DATA = 0.2
    t0 = time.monotonic()
    with pytest.raises(ProviderError) as ei:
        p.chat_stream("m", [], lambda t: None)
    assert "zaman aşımı" in str(ei.value) and "ilk" in str(ei.value) and time.monotonic() - t0 < 1.5
