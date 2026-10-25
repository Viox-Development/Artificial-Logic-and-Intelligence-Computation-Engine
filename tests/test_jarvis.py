"""Hafıza v2 (anlam araması, özet, takipler), projeler, sesli düşünme ve "iş bitmeden durma" kontrolü."""

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


# ----------------------------------------------------------------------------------------- vektörler
class FakeEmbedder:
    """Kelimeye göre sabit yön: 'proje/unity/oyun' aynı yönde, 'kahve/çay' başka yönde."""

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
    assert [h[0] for h in hits] == ["a", "b"]  # boyutu uymayan vektör atlanır


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
    assert not e.available  # hata sonrası bir süre denenmez: konuşma turları beklemesin


def test_semantic_search_finds_without_common_words():
    m = LongMemory(":memory:", {}, embedder=FakeEmbedder())
    m.remember("Kullanıcı Unity'de bir FPS yapıyor.", "fact")
    m.remember("Kullanıcı kahveyi şekersiz içer.", "preference")
    m.index_pending()
    assert m.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 2
    hits = m.search("geliştirdiğim şey neydi")
    assert hits and "FPS" in hits[0].item.text
    plain = LongMemory(":memory:", {})
    plain.remember("Kullanıcı Unity'de bir FPS yapıyor.", "fact")
    assert plain.search("geliştirdiğim şey neydi") == []  # kelime aramasıyla bulunamıyordu


def test_vectors_follow_updates_and_deletes():
    emb = FakeEmbedder()
    m = LongMemory(":memory:", {}, embedder=emb)
    fid, _ = m.remember("Kullanıcı kahve sever.", "preference")
    m.index_pending()
    m.update(fid, "Kullanıcı Unity projesiyle uğraşıyor.")
    m.index_pending()
    assert "Unity" in m.search("geliştirdiğim proje")[0].item.text
    m.forget(fid)
    assert m.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0


# ----------------------------------------------------------------------------------------- özet ve takipler
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
    assert any("FPS" in f.text for f in m.facts())
    prof = m.profile()
    assert "Earlier conversations" in prof and "Silah sistemine" in prof
    assert "Matematik sınavının" not in prof  # günü gelmedi
    later = time.time() + 2 * 86400
    assert any("Matematik" in f.text for f in m.due_followups(later))
    assert m.digest() == 0  # aynı konuşma iki kez özetlenmez


def test_digest_waits_for_conversation_to_end_and_skips_trivial():
    calls = []
    m = LongMemory(":memory:", {}, lambda p: calls.append(p) or "{}")
    m.record("saat kaç", "Üç.")
    assert m.pending_sessions() == []  # konuşma sürüyor
    _old(m, 1)
    assert len(m.pending_sessions()) == 1
    assert m.digest() == 0 and calls == []  # tek kısa tur: modele gitmez, ama işaretlenir
    assert m.pending_sessions() == []


def test_digest_failure_backs_off_and_retries_later():
    def boom(prompt):
        raise RuntimeError("kota")

    m = LongMemory(":memory:", {}, boom)
    m.record("bugün uzun bir konuşma yaptık, projeyi baştan planladık ve bir sürü karar verdik", "Evet.")
    _old(m, 1)
    assert m.digest() == 0
    assert float(m._meta("digest_fail_until")) > time.time()
    assert len(m.pending_sessions()) == 1  # kaybolmadı, sonra tekrar denenecek


def test_followup_lifecycle_and_expiry():
    m = LongMemory(":memory:", {})
    yesterday = time.time() - 86400
    a = m.add_followup("Maçın nasıl geçtiğini sor.", yesterday, "ask")
    assert m.add_followup("Maçın nasıl geçtiğini sor.", yesterday, "ask") == a  # aynısı iki kez eklenmez
    old = m.add_followup("Eski sınavı sor.", time.time() - 10 * 86400, "ask")
    due = m.due_followups()
    assert [f.id for f in due] == [a]
    assert old not in [f.id for f in m.followups()]  # çok eskisi kendiliğinden düştü
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
        tool.run(agent.ctx, "followup", note="Sınavı sor.", followup_kind="ask")  # günsüz "ask" olmaz


# ----------------------------------------------------------------------------------------- projeler
def _make_projects(home: Path) -> None:
    game = home / "GitHub" / "SkyFighter"
    (game / "Assets" / "Scripts").mkdir(parents=True)
    (game / "Assets" / "Scripts" / "Gun.cs").write_text("class Gun {}")
    (game / "ProjectSettings").mkdir()
    (game / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.23f1\n")
    old = time.time() - 30 * 86400  # oyun bir ay önce, bot az önce çalışıldı
    for f in game.rglob("*"):
        os.utime(f, (old, old))
    bot = home / "Projects" / "discord-bot"
    (bot / ".git" / "logs").mkdir(parents=True)
    (bot / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (bot / ".git" / "config").write_text('[remote "origin"]\n\turl = https://ghp_SECRET@github.com/hayat/discord-bot.git\n')
    now = int(time.time())
    (bot / ".git" / "logs" / "HEAD").write_text(
        f"0000 1111 Hayat <h@x> {now - 7200} +0300\tcommit (initial): ilk sürüm\n"
        f"1111 2222 Hayat <h@x> {now - 60} +0300\tcommit: müzik komutu eklendi\n")
    (bot / "main.py").write_text("print('bot')\n")
    (bot / "README.md").write_text("# Bot\n\nArkadaşlarla kullandığımız Discord müzik ve moderasyon botu.\n")
    (home / "Documents" / "notlar").mkdir(parents=True)  # proje değil


def test_scan_detects_projects_and_cleans_secrets(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _make_projects(home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    found = projects.scan(None)
    by = {p.name: p for p in found}
    assert set(by) == {"SkyFighter", "discord-bot"}
    bot, game = by["discord-bot"], by["SkyFighter"]
    assert found[0].name == "discord-bot"  # en son çalışılan önce
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
    assert projects.load()[0] > 0  # önbelleğe yazıldı
    assert "discord-bot" in projects.brief(None) and "SkyFighter" in projects.names(None)
    agent, _, _ = make_agent([], workdir)
    tool = Projects()
    assert "unity" in tool.run(agent.ctx, "info", query="skyfighter").text.lower()
    assert "eşleşen proje yok" in tool.run(agent.ctx, "find", query="zzzz").text


# ----------------------------------------------------------------------------------------- ajan
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
    agent3, _, _ = make_agent(["Yarın bakacağım."], workdir)  # araç kullanmadan sohbet: dokunma
    assert agent3.run_turn("sence?") == "Yarın bakacağım."
    agent4, _, _ = make_agent([tool_call("list_dir", {"path": "."}), "Şimdi bakacağım."], workdir, finish_nudges=0)
    assert agent4.run_turn("bak") == "Şimdi bakacağım."


# ----------------------------------------------------------------------------------------- ses
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


# ----------------------------------------------------------------------------------------- sağlayıcı
def test_cloudflare_needs_account_id(monkeypatch):
    from alice.config import load_config

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    assert not load_config().providers()["cloudflare"].enabled
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")
    pc = load_config().providers()["cloudflare"]
    assert pc.enabled and "/accounts/acc123/ai/v1" in pc.base_url
    assert "@cf/zai-org/glm-4.7-flash" in pc.models and not any("kimi" in m for m in pc.models)  # Kimi paralı


def test_old_cloudflare_list_with_paid_kimi_is_replaced(monkeypatch):
    # Eski `alice init` config.toml'a Kimi'li listeyi yazmıştı; kullanıcı dokunmadıysa yeni liste geçerli.
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
    assert load_config().data["providers"]["cloudflare"]["models"][0] == "@cf/benim/modelim"  # elle değiştirdiyse kalır


def test_model_not_on_free_plan_does_not_kill_whole_provider():
    from alice.providers.openai_compat import classify_error

    body = '{"errors":[{"message":"AiError: Model @cf/moonshotai/kimi-k2.6 is not available on the Workers Free plan"}]}'
    assert classify_error(403, body).kind == "not_found"
    assert classify_error(401, '{"error":"invalid api key"}').kind == "auth"
