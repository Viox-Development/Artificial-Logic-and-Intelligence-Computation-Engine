"""Memory v2 (semantic search, summaries, follow-ups), projects, voice thinking, and the "iş bitmeden durma" check."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from alice import projects
from alice.memory import LongMemory
from alice.memvec import Embedder, normalize, pack, top, unpack
from alice.tools.base import ToolError
from alice.tools.meta import Memory, Projects
from alice.voice.assistant import needs_thinking
from alice.voice.live import THINK_DECL, LiveSession
from tests.conftest import tool_call
from tests.test_agent import make_agent


# ----------------------------------------------------------------------------------------- vectors
class FakeEmbedder:
    """Fixed direction per word: 'proje/unity/oyun' point the same way, 'kahve/çay' point elsewhere."""

    available = True

    def __init__(self):
        self.calls = 0

    def _vec(self, text: str) -> list[float]:
        low = text.lower()
        if any(w in low for w in ("unity", "oyun", "proje", "geliştir")):
            return normalize([1.0, 0.05, 0.0])
        if any(w in low for w in ("kahve", "çay", "içecek")):
            return normalize([0.0, 1.0, 0.05])
        return normalize([0.0, 0.05, 1.0])

    def embed(self, texts):
        self.calls += 1
        return [self._vec(t) for t in texts]

    def query(self, text):
        return self._vec(text)


def test_pack_and_top():
    a, b = normalize([1, 0]), normalize([0.6, 0.8])
    assert unpack(pack(a)) == [1.0, 0.0]
    hits = top(a, [("a", pack(a)), ("b", pack(b)), ("x", pack([1.0, 0.0, 0.0]))], k=5, min_score=0.5)
    assert [h[0] for h in hits] == ["a", "b"]  # a vector with a mismatched size is skipped


def test_embedder_posts_and_backs_off():
    class Resp:
        def __init__(self, code, data=None):
            self.status_code, self._data, self.text = code, data, "boom"

        def json(self):
            return self._data

    class Client:
        def __init__(self):
            self.n = 0

        def post(self, url, json, headers, timeout):
            self.n += 1
            if self.n == 1:
                return Resp(200, {"data": [{"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 0]}]})
            return Resp(500)

    c = Client()
    e = Embedder("https://x/v1/", "k", "m", client=c)
    assert e.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    try:
        e.embed(["c"])
    except RuntimeError:
        pass
    assert not e.available  # after an error nothing is tried for a while, so conversation turns don't wait


def test_semantic_search_finds_without_common_words():
    m = LongMemory(":memory:", {}, embedder=FakeEmbedder())
    now = time.time()
    m.add_summary(now - 600, now - 500, "Kullanıcı Unity'de bir FPS yapıyor.")
    m.add_summary(now - 400, now - 300, "Kullanıcı kahveyi şekersiz içer.")
    m.index_pending()
    assert m.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 2
    hits = m.search("geliştirdiğim şey neydi")
    assert hits and "FPS" in hits[0].item.text
    plain = LongMemory(":memory:", {})
    plain.add_summary(now - 600, now - 500, "Kullanıcı Unity'de bir FPS yapıyor.")
    assert plain.search("geliştirdiğim şey neydi") == []  # keyword search could not find it before


def test_vectors_follow_deletes():
    m = LongMemory(":memory:", {}, embedder=FakeEmbedder())
    now = time.time()
    sid = m.add_summary(now - 600, now - 500, "Kullanıcı Unity projesiyle uğraşıyor.")
    m.index_pending()
    assert "Unity" in m.search("geliştirdiğim proje")[0].item.text
    m.forget_summary(sid)
    assert m.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0


# ----------------------------------------------------------------------------------------- summary and follow-ups
def _old(m: LongMemory, hours: float) -> None:
    m.db.execute("UPDATE episodes SET ts = ts - ?", (hours * 3600,))
    m.db.commit()


def test_digest_summarizes_and_creates_followups():
    seen = []
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    def extractor(prompt):
        seen.append(prompt)
        return json.dumps({
            "summary": "Matematik sınavına çalışma planı yapıldı; Unity FPS projesinde silah sistemi yarım kaldı.",
            "add": [{"text": "Kullanıcı Unity'de bir FPS oyunu geliştiriyor.", "kind": "fact"}],
            "followups": [{"text": "Matematik sınavının nasıl geçtiğini sor.", "kind": "ask", "due": tomorrow},
                          {"text": "Silah sistemine devam etmeyi teklif et.", "kind": "open"}],
            "done": []})

    m = LongMemory(":memory:", {}, extractor)
    m.record("yarın matematik sınavım var, plan yapalım", "Tamam, üç başlık çıkardım.", "ses")
    m.record("bu arada Unity'deki FPS'te silah sistemini yazıyordum, yarım kaldı", "Kaldığın yeri not ettim.", "yazı")
    _old(m, 2)
    assert m.digest() == 1
    assert "matematik" in seen[0] and "FPS" in seen[0]
    assert m.summaries()[0].text.startswith("Matematik sınavına")
    assert m.notes.scan() == []  # a summary writes no note: permanent notes come from end-of-turn extraction and Dream
    prof = m.profile()
    assert "Earlier conversations" in prof and "Silah sistemine" in prof
    assert "Matematik sınavının" not in prof  # its day has not come yet
    later = time.time() + 2 * 86400
    assert any("Matematik" in f.text for f in m.due_followups(later))
    assert m.digest() == 0  # the same conversation is not summarized twice


def test_digest_waits_for_conversation_to_end_and_skips_trivial():
    calls = []
    m = LongMemory(":memory:", {}, lambda p: calls.append(p) or "{}")
    m.record("saat kaç", "Üç.")
    assert m.pending_sessions() == []  # conversation still ongoing
    _old(m, 1)
    assert len(m.pending_sessions()) == 1
    assert m.digest() == 0 and calls == []  # single short turn: not sent to the model, but marked
    assert m.pending_sessions() == []


def test_digest_failure_backs_off_and_retries_later():
    def boom(prompt):
        raise RuntimeError("kota")

    m = LongMemory(":memory:", {}, boom)
    m.record("bugün uzun bir konuşma yaptık, projeyi baştan planladık ve bir sürü karar verdik", "Evet.")
    _old(m, 1)
    assert m.digest() == 0
    assert float(m._meta("digest_fail_until")) > time.time()
    assert len(m.pending_sessions()) == 1  # not lost, it will be retried later
    for _ in range(5):  # network/quota errors never cause the conversation to be skipped
        m.digest()
    assert len(m.pending_sessions()) == 1


def test_digest_skips_a_conversation_the_model_never_answers_usably():
    calls = []

    def extractor(prompt):
        calls.append(prompt)
        if "Unity" in prompt:
            return "{}"  # the model always gives an empty/broken answer for this conversation
        return json.dumps({"summary": "Matematik sınavı planlandı.", "followups": [], "done": []})

    m = LongMemory(":memory:", {}, extractor)
    m.record("Unity projesinde silah sistemini yazıyorduk, uzun uzun konuştuk", "Evet.")
    _old(m, 3)
    m.record("matematik sınavına nasıl çalışacağımızı planlayalım bugün", "Tamam.")
    _old(m, 1)
    for _ in range(3):
        m._meta("digest_fail_until", "0")
        m.digest()
    # A conversation that keeps giving unusable answers is skipped after a few attempts; the next conversation
    # gets summarized (it used to hang forever).
    assert [s.text for s in m.summaries()] == ["Matematik sınavı planlandı."]
    assert m.pending_sessions() == []


def test_followup_lifecycle_and_expiry():
    m = LongMemory(":memory:", {})
    yesterday = time.time() - 86400
    a = m.add_followup("Maçın nasıl geçtiğini sor.", yesterday, "ask")
    assert m.add_followup("Maçın nasıl geçtiğini sor.", yesterday, "ask") == a  # the same item is not added twice
    old = m.add_followup("Eski sınavı sor.", time.time() - 10 * 86400, "ask")
    due = m.due_followups()
    assert [f.id for f in due] == [a]
    assert old not in [f.id for f in m.followups()]  # one that is too old dropped off by itself
    assert "Follow-ups due" in m.profile()
    assert m.done_followup(a).text.startswith("Maçın")
    assert m.due_followups() == []


def test_memory_tool_followups(workdir):
    from alice.memory import attach_memory

    agent, _, _ = make_agent([], workdir)
    attach_memory(agent, "yazı")
    tool = Memory()
    day = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    out = tool.run(agent.ctx, "followup", note="Mülakatın nasıl geçtiğini sor.", due=day)
    assert out.text.startswith("takip eklendi #")
    fid = int(out.text.rsplit("#", 1)[1])
    assert "Mülakat" in tool.run(agent.ctx, "followups").text
    assert "kapatıldı" in tool.run(agent.ctx, "done", id=fid).text
    with pytest.raises(ToolError):
        tool.run(agent.ctx, "followup", note="Sınavı sor.", followup_kind="ask")  # an "ask" without a date is not allowed


# ----------------------------------------------------------------------------------------- projects
def _make_projects(home: Path) -> None:
    game = home / "GitHub" / "SkyFighter"
    (game / "Assets" / "Scripts").mkdir(parents=True)
    (game / "Assets" / "Scripts" / "Gun.cs").write_text("class Gun {}", encoding="utf-8")
    (game / "ProjectSettings").mkdir()
    (game / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.23f1\n", encoding="utf-8")
    old = time.time() - 30 * 86400  # the game was a month ago, the bot was worked on just now
    for f in game.rglob("*"):
        os.utime(f, (old, old))
    bot = home / "Projects" / "discord-bot"
    (bot / ".git" / "logs").mkdir(parents=True)
    (bot / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (bot / ".git" / "config").write_text('[remote "origin"]\n\turl = https://ghp_SECRET@github.com/hayat/discord-bot.git\n', encoding="utf-8")
    now = int(time.time())
    (bot / ".git" / "logs" / "HEAD").write_text(
        f"0000 1111 Hayat <h@x> {now - 7200} +0300\tcommit (initial): ilk sürüm\n"
        f"1111 2222 Hayat <h@x> {now - 60} +0300\tcommit: müzik komutu eklendi\n", encoding="utf-8")
    (bot / "main.py").write_text("print('bot')\n", encoding="utf-8")
    (bot / "README.md").write_text("# Bot\n\nArkadaşlarla kullandığımız Discord müzik ve moderasyon botu.\n", encoding="utf-8")
    (home / "Documents" / "notlar").mkdir(parents=True)  # not a project


def test_scan_detects_projects_and_cleans_secrets(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _make_projects(home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    found = projects.scan(None)
    by = {p.name: p for p in found}
    assert set(by) == {"SkyFighter", "discord-bot"}
    bot, game = by["discord-bot"], by["SkyFighter"]
    assert found[0].name == "discord-bot"  # most recently worked on first
    assert bot.remote == "hayat/discord-bot" and "SECRET" not in json.dumps(bot.__dict__)
    assert bot.last_commit == "müzik komutu eklendi" and bot.branch == "main"
    assert bot.languages == ["Python"] and bot.readme.startswith("Arkadaşlarla")
    assert game.kind == "unity 6000.0.23f1" and game.languages == ["C#"]
    assert projects.find("discord", found)[0] is bot
    info = projects.info(bot)
    assert "ilk sürüm" in info and "main.py" in info


def test_projects_cache_brief_and_tool(tmp_path, monkeypatch, workdir):
    home = tmp_path / "home"
    _make_projects(home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(projects, "enabled", lambda cfg=None: True)
    assert projects.get(None, background=False)[0].name == "discord-bot"
    assert projects.load()[0] > 0  # written to the cache
    assert "discord-bot" in projects.brief(None) and "SkyFighter" in projects.names(None)
    agent, _, _ = make_agent([], workdir)
    tool = Projects()
    assert "unity" in tool.run(agent.ctx, "info", query="skyfighter").text.lower()
    assert "eşleşen proje yok" in tool.run(agent.ctx, "find", query="zzzz").text


# ----------------------------------------------------------------------------------------- agent
def test_agent_continues_when_todo_left_unfinished(workdir):
    todos = [{"content": "Dosyayı yaz", "status": "done"}, {"content": "Testleri çalıştır", "status": "pending"}]
    script = [tool_call("todo_write", {"todos": todos}), "Dosyayı yazdım.",
              tool_call("todo_write", {"todos": [{**t, "status": "done"} for t in todos]}, cid="c2"),
              "Testler de geçti, bitti."]
    agent, provs, _ = make_agent(script, workdir)
    assert agent.run_turn("dosyayı yaz ve test et") == "Testler de geçti, bitti."
    nudges = [m for m in agent.messages if m.get("_nudge")]
    assert len(nudges) == 1 and "Testleri çalıştır" in nudges[0]["content"]


def test_agent_nudges_promise_but_not_questions_or_chat(workdir):
    script = [tool_call("list_dir", {"path": "."}), "Şimdi testleri çalıştıracağım.", "Çalıştırdım, hepsi geçti."]
    agent, _, _ = make_agent(script, workdir)
    assert agent.run_turn("testleri çalıştır") == "Çalıştırdım, hepsi geçti."
    agent2, _, _ = make_agent([tool_call("list_dir", {"path": "."}), "Hangisini silmemi istersin?"], workdir)
    assert agent2.run_turn("dosyalara bak").endswith("?")
    agent3, _, _ = make_agent(["Yarın bakacağım."], workdir)  # chat without using tools: leave it alone
    assert agent3.run_turn("sence?") == "Yarın bakacağım."
    agent4, _, _ = make_agent([tool_call("list_dir", {"path": "."}), "Şimdi bakacağım."], workdir, finish_nudges=0)
    assert agent4.run_turn("bak") == "Şimdi bakacağım."


# ----------------------------------------------------------------------------------------- voice
def test_needs_thinking_routes_questions_not_commands():
    assert needs_thinking("Unity'de raycast neden çalışmıyor")
    assert needs_thinking("F-16 ile F-15 arasındaki farkı anlat")
    assert needs_thinking("bu hafta için bir çalışma planı yapar mısın")
    assert not needs_thinking("YouTube'u aç")
    assert not needs_thinking("sesi biraz kıs")
    assert not needs_thinking("")


def test_live_think_uses_brain_and_history(workdir):
    agent, provs, _ = make_agent(["F-16 tek motorlu, F-15 çift motorlu; F-15 daha ağır ve menzili uzun."], workdir)
    s = LiveSession(SimpleNamespace(data={"voice": {}, "providers": {}}), agent)
    s._history = [(time.time() - 60, "F-16'dan bahsediyorduk", "Evet, Viper.")]
    answer = s.think_answer("F-16 ile F-15'in farkı ne")
    assert answer.startswith("F-16 tek motorlu")
    sent = provs["a"].calls[0]
    user = sent["messages"][-1]["content"]
    assert "F-15'in farkı" in user and "Viper" in user
    names = {t["function"]["name"] for t in sent["tools"]}
    assert names <= {"web_search", "web_fetch", "read_file", "list_dir", "glob", "grep", "memory", "projects",
                     "weather", "system_info"} and "web_search" in names


def test_live_setup_declares_think_and_history():
    agent = SimpleNamespace(messages=[{"role": "system", "content": "sys"}], ctx=SimpleNamespace(state={}),
                            tool_specs=lambda: [])
    s = LiveSession(SimpleNamespace(data={"voice": {}, "providers": {}}), agent)
    s.names = lambda: []
    s._history = [(time.time() - 30, "yarınki sınavı konuştuk", "Çalışma planı yaptık.")]
    setup = s._setup("m")["setup"]
    assert THINK_DECL in setup["tools"][0]["functionDeclarations"]
    text = setup["systemInstruction"]["parts"][0]["text"]
    assert "Az önceki konuşma" in text and "yarınki sınavı" in text and "Ne zaman think" in text
    s.think = False
    s._history = [(time.time() - 7200, "eski", "")]
    setup = s._setup("m")["setup"]
    assert THINK_DECL not in setup["tools"][0]["functionDeclarations"]
    assert "Az önceki konuşma" not in setup["systemInstruction"]["parts"][0]["text"]


# ----------------------------------------------------------------------------------------- provider
def test_cloudflare_needs_account_id(monkeypatch):
    from alice.config import load_config

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    assert not load_config().providers()["cloudflare"].enabled
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")
    pc = load_config().providers()["cloudflare"]
    assert pc.enabled and "/accounts/acc123/ai/v1" in pc.base_url
    assert "@cf/zai-org/glm-4.7-flash" in pc.models and not any("kimi" in m for m in pc.models)  # Kimi is paid


def test_old_cloudflare_list_with_paid_kimi_is_replaced(monkeypatch):
    # An old `alice init` had written the Kimi list into config.toml; if the user did not touch it, the new list applies.
    from alice.config import LEGACY_DEFAULTS, alice_home, init_home, load_config

    old = next(v for (sec, key), v in LEGACY_DEFAULTS if sec == "providers.cloudflare")
    init_home()
    p = alice_home() / "config.toml"
    p.write_text(p.read_text(encoding="utf-8").replace(
        'models = ["@cf/zai-org/glm-4.7-flash"', 'models = ["@cf/moonshotai/kimi-k2.5", "@cf/zai-org/glm-4.7-flash"', 1),
        encoding="utf-8")
    assert old[0] in p.read_text(encoding="utf-8")
    assert not any("kimi" in m for m in load_config().data["providers"]["cloudflare"]["models"])
    p.write_text(p.read_text(encoding="utf-8").replace(old[0], "@cf/benim/modelim", 1), encoding="utf-8")
    assert load_config().data["providers"]["cloudflare"]["models"][0] == "@cf/benim/modelim"  # stays if the user changed it by hand


def test_retired_glm_brain_chain_from_old_init_is_replaced():
    # `alice init` used to write the GLM 5.3 chain into config.toml; NVIDIA retired the model.
    from alice.config import alice_home, init_home, load_config

    init_home()
    p = alice_home() / "config.toml"
    text = p.read_text(encoding="utf-8")
    old_line = 'brain = ["nvidia:z-ai/glm-5.3", "auto", "ollama:qwen3:8b"]'
    p.write_text(text.replace('brain = ["auto", "ollama:qwen3:8b"]', old_line, 1), encoding="utf-8")
    assert old_line in p.read_text(encoding="utf-8")
    assert load_config().data["roles"]["brain"] == ["auto", "ollama:qwen3:8b"]
    p.write_text(p.read_text(encoding="utf-8").replace(old_line, 'brain = ["nvidia:z-ai/glm-5.3"]', 1),
                 encoding="utf-8")
    assert load_config().data["roles"]["brain"] == ["nvidia:z-ai/glm-5.3"]  # a hand-edited chain stays


def test_model_not_on_free_plan_does_not_kill_whole_provider():
    from alice.providers.openai_compat import classify_error

    body = '{"errors":[{"message":"AiError: Model @cf/moonshotai/kimi-k2.6 is not available on the Workers Free plan"}]}'
    assert classify_error(403, body).kind == "not_found"
    assert classify_error(401, '{"error":"invalid api key"}').kind == "auth"
