from __future__ import annotations

import json
import time

from alice.memory import LongMemory, attach_memory, clean_user_text, explicit_note, has_signal, terms
from alice.prompts import MEMORY_OPEN, with_memory
from alice.tools import ToolContext
from alice.tools.meta import Memory
from tests.test_agent import make_agent


def mem(**opts) -> LongMemory:
    return LongMemory(":memory:", opts)


def notes(m: LongMemory) -> list:
    return m.notes.scan()


def test_terms_fold_turkish_suffixes():
    assert "kahv" in terms("Kahveyi şekersiz içerim")
    assert set(terms("KAHVEMİ")) == {"kahv"}
    assert terms("filmini") == terms("film")
    assert terms("ve bu bir şey") == []


def test_remember_writes_typed_note_dedupes_and_profile_shows_index_and_rules():
    m = mem()
    a, new_a = m.remember("Kullanıcı kahveyi şekersiz içer.", "preference")
    b, new_b = m.remember("Kullanıcı kahveyi şekersiz içer", "preference")
    assert new_a and not new_b and a == b
    m.remember("Kullanıcı oyunu kapatmadan önce sorulmasını ister.", "preference")
    types = {n.type for n in notes(m)}
    assert types == {"user", "feedback"}  # a persistent instruction becomes a feedback note
    prof = m.profile()
    assert "Long-term memory" in prof and "MEMORY.md" in prof and "Where a note goes" in prof
    assert f"]({a}.md)" in prof and "sorulmasını" in prof  # index + full text of the feedback note
    assert sum(line.startswith("- [") for line in m.notes.index_raw().splitlines()) == 2


def test_search_notes_and_old_conversation():
    m = mem()
    m.remember("Kullanıcı en çok Interstellar filmini sever.", "preference")
    m.record("Hey Alice, yarın Ankara'ya trenle gidiyorum, bilet fiyatlarına bak\n\n[şu an: 01.10.2026 ...]",
             "Bilet 450 lira.", "ses")
    assert "Interstellar" in m.search_notes("en sevdiğim film interstellar")[0].body
    hits = m.search("ankara treni ne kadardı")
    assert hits and "Ankara" in hits[0].item.user and "[şu an" not in hits[0].item.user
    assert m.search("hava nasıl") == []


def test_recall_attaches_whole_note_once_and_strips_cleanly():
    m = mem()
    key, _ = m.remember("Kullanıcı Opera GX tarayıcısını kullanır.", "fact")
    seen: set[str] = set()
    block = m.recall("opera gx tarayıcısını aç", surfaced=seen)
    assert block.startswith("[hafızadan") and "Opera GX" in block
    assert m.recall("opera gx tarayıcısını aç", surfaced=seen) == ""  # not added a second time in this chat
    assert clean_user_text("merhaba\n\n" + block) == "merhaba"


def test_recall_lets_the_fast_model_pick_and_warns_about_old_notes():
    asked = []

    def chooser(prompt: str) -> str:
        asked.append(prompt)
        return json.dumps({"selected": ["genel:feedback-testler"]})

    m = LongMemory(":memory:", {}, chooser)
    m.save_note("feedback-testler", "Yalnız değişen koda ait testleri koş", "feedback",
                "Tüm suite'i değil ilgili testleri koş.\n**Neden:** suite çok uzun.\n**Nasıl uygulanır:** her değişiklikte.")
    m.save_note("user-oyunlar", "Oynadığı oyunlar: War Thunder, Unciv", "user", "War Thunder ve Unciv oynar.")
    import os
    import re
    from datetime import datetime

    old = m.notes.path_for("feedback-testler")
    then = time.time() - 40 * 86400
    old.write_text(re.sub(r"modified: .*", f"modified: {datetime.fromtimestamp(then).isoformat(timespec='seconds')}",
                          old.read_text(encoding="utf-8")), encoding="utf-8")
    os.utime(old, (then, then))
    block = m.recall("testleri çalıştır")
    assert "genel:feedback-testler" in asked[0] and "Unciv" in asked[0]  # descriptions were shown to the model
    assert "**Neden:**" in block and "War Thunder" not in block
    assert "gün önce yazıldı" in block  # old note: warning to check its current state


def test_explicit_note_and_signals():
    assert explicit_note("Alice, bunu hatırla: annemin doğum günü 3 Mayıs") == "Annemin doğum günü 3 Mayıs"
    assert explicit_note("arabamı B3 katına park ettim, unutma") == "Arabamı B3 katına park ettim"
    assert explicit_note("saat kaç") is None
    assert has_signal("Ben caz müziği çok severim")
    assert not has_signal("YouTube'u aç")
    m = mem()
    m.learn("bunu hatırla: annemin doğum günü 3 Mayıs")
    n = notes(m)[0]
    assert "3 Mayıs" in n.body and n.source == "kullanıcı"


def test_learn_extracts_typed_notes_and_updates_existing_by_name():
    seen = []

    def extractor(prompt: str) -> str:
        seen.append(prompt)
        if "çay" in prompt:
            return json.dumps({"save": [{"name": "user-icecek", "type": "user", "description": "Artık çay içiyor",
                                         "content": "Kullanıcı kahveyi bıraktı, çay içiyor."}]})
        return ('```json\n{"save": [{"name": "user-icecek", "type": "user", "description": "Kahveyi şekersiz içer",'
                ' "content": "Kullanıcı kahveyi şekersiz içer."}]}\n```')

    m = LongMemory(":memory:", {}, extractor)
    m.record("YouTube'u aç", "Açtım.")
    m.learn("YouTube'u aç", background=False)
    assert not seen  # no signal and no turn either: the model is not called
    m.record("ben kahveyi hep şekersiz içerim", "Not ettim.")
    m.learn("ben kahveyi hep şekersiz içerim", "Not ettim.", background=False)
    assert [n.description for n in notes(m)] == ["Kahveyi şekersiz içer"]
    m.record("artık kahve içmiyorum, çay seviyorum", "Tamam.")
    m.learn("artık kahve içmiyorum, çay seviyorum", background=False)
    assert "genel:user-icecek" in seen[-1] and "Kahveyi şekersiz" in seen[-1]  # the existing note was shown to the model
    assert [n.description for n in notes(m)] == ["Artık çay içiyor"]  # updated under the same name, no duplicate
    assert "artık kahve" in seen[-1] and "YouTube" not in seen[-1]  # cursor: turns already checked are not sent again


def test_feedback_and_confirmation_trigger_extraction_and_own_write_skips_it():
    calls = []
    m = LongMemory(":memory:", {}, lambda p: calls.append(p) or "{}")
    m.record("hayır öyle değil, testleri tek tek koşma", "Tamam.")
    m.learn("hayır öyle değil, testleri tek tek koşma", background=False)
    m.record("aynen böyle devam", "Peki.")
    m.learn("aynen böyle devam", background=False)
    assert len(calls) == 2  # both the correction and the confirmation are feedback signals
    m.record("bunu feedback olarak kaydettim", "")
    m.learn("hayır, bundan sonra hep sor", wrote=True, background=False)
    assert len(calls) == 2  # ALICE wrote to memory itself this turn, so background extraction is skipped


def test_one_off_things_are_not_saved_as_user_notes_and_requested_notes_are_protected():
    m = mem()
    done = m.apply_notes({"save": [
        {"name": "user-deneme", "type": "user", "description": r"C:\Users\x\Desktop\Deneme klasörünü açtı",
         "content": "..."},
        {"name": "user-kahve", "type": "user", "description": "Kahveyi şekersiz içer", "content": "Şekersiz."},
        {"name": "bad", "type": "secret", "description": "x", "content": "y"},
    ]})
    assert [n.name for n in notes(m)] == ["user-kahve"] and done == ["added genel:user-kahve"]
    m.remember("Kullanıcının adı Hayat.", "fact", source="kullanıcı")
    keep = [n for n in notes(m) if n.source == "kullanıcı"][0]
    m.apply_notes({"delete": [keep.key, "genel:user-kahve"]})
    assert {n.name for n in notes(m)} == {keep.name}  # the explicitly requested note was not deleted


def test_import_markdown_once(tmp_path):
    md = tmp_path / "memory.md"
    md.write_text("* Kullanıcının adı Hayat  _(2026-09-01)_\n* Projeler D:\\Unity altında  _(2026-09-02)_\n", encoding="utf-8")
    m = mem()
    assert m.import_markdown(md) == 2
    assert m.import_markdown(md) == 0
    assert {n.body for n in notes(m)} == {"Kullanıcının adı Hayat", "Projeler D:\\Unity altında"}


def test_old_facts_migrate_to_notes_once():
    m = mem()
    now = time.time()
    with m._lock:
        m.db.execute("INSERT INTO facts(created, updated, kind, text, terms, source) VALUES (?, ?, ?, ?, '', ?)",
                     (now, now, "preference", "Kullanıcı GitHub işlerinde kendi gh oturumunun kullanılmasını ister.",
                      "otomatik"))
        m.db.execute("INSERT INTO facts(created, updated, kind, text, terms, source) VALUES (?, ?, ?, ?, '', ?)",
                     (now, now, "person", "Kanin, kullanıcının yakın bir arkadaşıdır.", "araç"))
        m.db.commit()
    assert m.migrate_facts() == 2 and m.migrate_facts() == 0
    by = {n.type: n for n in notes(m)}
    assert "gh oturumunun" in by["feedback"].body and by["user"].source == "kullanıcı"


def test_partial_fact_migration_is_retried_without_overwriting_migrated_notes(monkeypatch):
    m = mem()
    now = time.time()
    with m._lock:
        for i, text in enumerate(["Kullanıcının kedisinin adı Pamuk.", "Kullanıcı sabahları kahve içer."]):
            m.db.execute("INSERT INTO facts(created, updated, kind, text, terms, source) VALUES (?, ?, ?, ?, '', ?)",
                         (now + i, now + i, "fact", text, "otomatik"))
        m.db.commit()
    real = m.remember

    def flaky(text, *a, **kw):
        if "kahve" in text:
            raise RuntimeError("dizin dolu")
        return real(text, *a, **kw)

    monkeypatch.setattr(m, "remember", flaky)
    assert m.migrate_facts() == 1
    assert not m._meta("notes_migrated")  # one could not be moved: not counted as done
    cat = next(n for n in notes(m) if "Pamuk" in n.body)
    m.notes.save(cat.name, cat.description, cat.type, "Kullanıcının kedisinin adı Pamuk, 3 yaşında.")  # manual edit
    monkeypatch.setattr(m, "remember", real)
    assert m.migrate_facts() == 1  # only the remaining ones were moved
    assert m._meta("notes_migrated") and m.migrate_facts() == 0
    bodies = {n.body for n in notes(m)}
    assert "Kullanıcının kedisinin adı Pamuk, 3 yaşında." in bodies and "Kullanıcı sabahları kahve içer." in bodies


def test_episodes_pruned_by_age():
    m = mem(keep_days=1)
    m.record("eski konuşma", "cevap")
    m.db.execute("UPDATE episodes SET ts = ?", (time.time() - 3 * 86400,))
    m._prune()
    assert m.recent() == []


def test_with_memory_replaces_section():
    s = with_memory("sys", "# Long-term memory\n- a")
    assert s.count(MEMORY_OPEN) == 1 and "- a" in s
    s2 = with_memory(s + "\n\n# Voice mode", "# Long-term memory\n- b")
    assert "- a" not in s2 and "- b" in s2 and s2.endswith("# Voice mode")


def test_agent_injects_recall_records_turns_and_refreshes_profile(workdir):
    agent, provs, _ = make_agent(["Tamam.", "Interstellar dedin."], workdir)
    attach_memory(agent, "yazı")
    assert agent.memory is not None
    agent.memory.recall_model = False
    agent.memory.remember("Kullanıcının en sevdiği film Interstellar.", "preference")
    agent.run_turn("en sevdiğim film interstellar mıydı?")
    sent = provs["a"].calls[0]["messages"]
    assert "Interstellar" in sent[0]["content"]  # index + user note are in the system prompt
    assert "[hafızadan" in sent[-1]["content"]
    assert agent.messages[1]["_display"] == "en sevdiğim film interstellar mıydı?"
    ep = agent.memory.recent(1)[0]
    assert ep.user == "en sevdiğim film interstellar mıydı?" and ep.reply == "Tamam." and ep.channel == "yazı"


def test_memory_tool_save_read_search_delete(workdir):
    agent, _, _ = make_agent([], workdir)
    attach_memory(agent, "yazı")
    ctx: ToolContext = agent.ctx
    tool = Memory()
    out = tool.run(ctx, "save", name="user-takim", type="user", description="Tuttuğu takım: Galatasaray",
                   content="Kullanıcı Galatasaray tutar.").text
    assert out.startswith("kaydedildi: genel:user-takim") and ctx.state["memory_wrote"]
    assert "Galatasaray" in tool.run(ctx, "read", name="user-takim").text
    assert "user-takim" in tool.run(ctx, "search", query="galatasaray").text
    assert "user-takim" in tool.run(ctx, "list").text
    assert tool.risk_for({"action": "delete"}) == "write" and tool.risk_for({"action": "save"}) == "read"
    assert tool.run(ctx, "delete", name="user-takim").text.startswith("silindi")
    assert agent.memory.notes.scan() == [] and "user-takim" not in agent.memory.notes.index_raw()
    assert any((agent.memory.notes.root / ".trash").glob("*user-takim.md"))  # in the trash, can be restored
    # old names still work
    assert "kaydedildi" in tool.run(ctx, "add", note="Kullanıcı Galatasaray tutar.", kind="preference").text
    assert tool.run(ctx, "forget", query="Galatasaray").text.startswith("silindi")


def _chat(m, ts, user, reply=""):
    with m._lock:
        m.db.execute("INSERT INTO episodes(ts, channel, user, reply, terms) VALUES (?, '', ?, ?, '')", (ts, user, reply))
        m.db.commit()


def test_gecmis_sohbet_arama_ve_son_sohbetler():
    m = mem()
    day = 86400
    now = time.time()
    for i, (u, r) in enumerate([("Unity'de silah sistemini yapıyoruz", "Raycast ile başlayalım"),
                                ("geri tepme de ekleyelim", "Tamam, recoil eklendi")]):
        _chat(m, now - 3 * day + i * 60, u, r)
    _chat(m, now - day, "yarın matematik sınavım var", "Başarılar!")
    with m._lock:  # terms column: for word search
        for row in m.db.execute("SELECT id, user, reply FROM episodes").fetchall():
            m.db.execute("UPDATE episodes SET terms = ? WHERE id = ?", (" ".join(terms(row[1] + " " + row[2])), row[0]))
        m.db.commit()
    found = m.search_chats("silah sistemi unity")
    assert "Raycast" in found and "recoil" in found and "sınav" not in found
    recent = m.recent_chats(1)
    assert "matematik" in recent and "silah" not in recent
    both = m.recent_chats(5)
    assert both.index("matematik") < both.index("silah")  # newest to oldest
    assert "silah" in m.recent_chats(5, before=now - 2 * day) and "matematik" not in m.recent_chats(5, before=now - 2 * day)
    assert "nothing about" in m.search_chats("uzay gemisi")
    # Old turns are no longer added to every message automatically; the model finds them with search_chats.
    assert "Raycast" not in m.recall("unity silah sistemi")


def test_hafiza_araci_gecmis_sohbet_eylemleri(workdir):
    m = mem()
    _chat(m, time.time() - 60, "dün kek yaptım", "Afiyet olsun")
    from tests.conftest import make_config

    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    ctx.state["memory"] = m
    assert "kek" in Memory().run(ctx, "recent_chats", n=3).text
    import pytest

    from alice.tools.base import ToolError

    with pytest.raises(ToolError):
        Memory().run(ctx, "search_chats")


def test_vektorler_parti_parti_kaydedilir_kota_dolunca_kalan_beklenir():
    from alice.memvec import RateLimited, normalize

    class Emb:
        available, calls = True, 0

        def embed(self, texts):
            self.calls += 1
            if self.calls > 1:
                raise RateLimited("429")  # per-minute quota: first batch passed, second hit the limit
            return [normalize([1.0, float(i)]) for i in range(len(texts))]

    m = LongMemory(":memory:", {}, None, Emb())
    for i in range(30):
        _chat(m, time.time() - i, f"konuşma {i}", "cevap")
    assert m.index_pending() == 20  # first batch saved; previously all of them were dropped
    assert m.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 20


def test_silinen_kaydin_vektoru_kalmaz(tmp_path):
    from alice.memvec import normalize

    class Emb:
        available = True

        def __init__(self, hook=None):
            self.hook = hook

        def embed(self, texts):
            if self.hook:
                self.hook()
            return [normalize([1.0, 0.5]) for _ in texts]

    m = LongMemory(tmp_path / "m.db", {"keep_days": 30}, None, None, notes_root=tmp_path / "notes")
    old = time.time() - 60 * 86400
    _chat(m, old, "eski konuşma", "cevap")
    _chat(m, time.time() - 5, "yeni konuşma", "cevap")
    gone = m.db.execute("SELECT id FROM episodes WHERE ts = ?", (old,)).fetchone()[0]
    keep = m.db.execute("SELECT id FROM episodes WHERE ts != ?", (old,)).fetchone()[0]
    # If a chat is deleted while embedding runs, don't write an orphaned vector.
    m.embedder = Emb(hook=lambda: m.forget_episode(keep))
    m.index_pending()
    refs = {r[0] for r in m.db.execute("SELECT ref FROM vectors WHERE kind = 'episode'")}
    assert keep not in refs and gone in refs
    m.close()
    # When old turns are pruned, their vectors go too (so they don't take the top spots in semantic search).
    m2 = LongMemory(tmp_path / "m.db", {"keep_days": 30}, None, None, notes_root=tmp_path / "notes")
    assert m2.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
    m2.close()


def test_surum_artisi_es_zamanli_cagrilarda_kaybolmaz():
    import threading

    m = mem()
    start = m.version

    def work():
        for _ in range(40):
            m._bump()

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert m.version == start + 240


def test_aractan_sonra_gelen_canli_cevap_soruya_eklenir():
    from types import SimpleNamespace

    from alice.voice.live import LiveSession

    m = mem()
    s = LiveSession.__new__(LiveSession)
    s.log, s.model, s._history = (lambda x: None), "m", []
    s.agent = SimpleNamespace(memory=m)
    ui = SimpleNamespace(tools=[])
    state = {"heard": ["Ankara'da hava nasıl?"], "said": []}
    s._flush_log(state, ui)  # the model went quiet first and then called a tool
    state["said"] = ["Ankara'da hava kapalı ve on iki derece."]
    s._flush_log(state, ui)  # the reply came in a separate turn
    ep = m.recent(1)[0]
    assert ep.user == "Ankara'da hava nasıl?" and "on iki derece" in ep.reply
    assert len(m.recent(5)) == 1 and "on iki derece" in s._history[-1][2]


def test_gurultu_kaydedilmez_ozetteki_kalip_cumleler_atilir():
    from alice.memory import strip_filler

    m = mem()
    assert m.record("हे या इनिशियलाइज़ेशन", "") is None and m.recent() == []
    assert m.record("Ankara'da hava nasıl?", "") is not None
    assert strip_filler("Kullanıcı sesinin değiştiğini söyledi. Hiçbir işlem gerçekleştirilmedi veya karar "
                        "alınmadı. Devam eden bir iş kalmadı.") == "Kullanıcı sesinin değiştiğini söyledi."
    assert strip_filler("Kullanıcı hava durumunu sordu; ALICE henüz yanıt vermedi.") == "Kullanıcı hava durumunu sordu."


# ---------------------------------------------------------------------------------------------------- Dream
def _dream_mem(answer, sessions: int = 5, **opts):
    seen = []

    def synth(prompt):
        seen.append(prompt)
        return json.dumps(answer) if not callable(answer) else answer(prompt)

    m = LongMemory(":memory:", {"dream_sessions": sessions, **opts}, lambda p: "{}")
    m.synthesizer = synth
    return m, seen


def test_dream_merges_fixes_prunes_and_keeps_a_backup():
    m, seen = _dream_mem({
        "save": [{"name": "user-oyunlar", "type": "user", "title": "Oyunlar",
                  "description": "Oynadığı oyunlar: Unciv, Solitaire", "content": "Unciv ve Solitaire oynar."}],
        "delete": ["user-unciv", "user-solitaire"],
        "index": "## Kullanıcı\n- [Oyunlar](user-oyunlar.md) — Unciv, Solitaire\n- [Yok](olmayan.md) — x",
        "summary": "İki oyun notu birleştirildi."})
    m.save_note("user-unciv", "Unciv oynuyor", "user", "Unciv oynuyor.")
    m.save_note("user-solitaire", "Solitaire sever", "user", "Solitaire sever.")
    m.remember("Kullanıcının adı Hayat.", "fact", source="kullanıcı")
    now = time.time()
    m.add_summary(now - 600, now - 500, "Kullanıcı Unciv'de savaş başlattı.")
    done = m.dream(force=True)
    names = {n.name for n in notes(m)}
    assert "user-oyunlar" in names and "user-unciv" not in names and "user-solitaire" not in names
    assert any("Hayat" in n.body for n in notes(m))  # the explicitly requested note was left untouched
    idx = m.notes.index_raw()
    # The model's proposed index is ignored: it is rebuilt from the notes (no dangling line, no missing note).
    assert idx.startswith("# Hafıza Dizini") and "olmayan.md" not in idx and "Hayat" in idx
    assert "](user-oyunlar.md)" in idx
    assert "Unciv'de savaş" in seen[0] and "(kullanıcı istedi)" in seen[0]  # summaries and protection marker reach the model
    assert any("İki oyun notu" in x for x in done) and m.notes.backups()  # backup taken
    assert "Dream:" in m.notes.logs_since(0)
    # can be rolled back to the backup
    m.notes.restore(m.notes.backups()[-1])
    assert {"user-unciv", "user-solitaire"} <= {n.name for n in notes(m)}


def test_dream_never_bulk_deletes_and_uses_the_strong_model():
    fast = []
    m, seen = _dream_mem({"delete": [f"user-meyve-{i}" for i in range(8)], "summary": "budandı"})
    m.extractor = lambda p: fast.append(p) or "{}"
    for i in range(8):
        m.save_note(f"user-meyve-{i}", f"meyve {i} sever", "user", f"{i}. meyveyi sever.")
    m.dream(force=True)
    assert len(notes(m)) == 6  # at most a quarter of 8 (2) were deleted
    assert seen and not fast  # Dream uses the strong model


def test_maybe_dream_waits_for_time_and_enough_conversations():
    m, seen = _dream_mem({"summary": "tamam"}, sessions=3)
    m.save_note("user-x", "x", "user", "x")
    now = time.time()
    for i in range(2):
        _chat(m, now - 10 * 3600 + i * 3600, f"konuşma {i}", "cevap")
    assert not m.maybe_dream() and not seen  # 2 conversations < 3
    m._last_dream_scan = None
    _chat(m, now - 5 * 3600, "üçüncü konuşma", "cevap")
    assert m.maybe_dream() and seen
    m._last_dream_scan = None
    assert not m.maybe_dream()  # 24 hours have not passed since the last Dream
    assert m.notes.last_dream() > now - 60


def test_dream_lock_blocks_a_second_process_and_failure_rolls_back_the_clock():
    import os

    m, _ = _dream_mem(lambda p: "bozuk cevap")
    m.save_note("user-x", "x", "user", "x")
    prior = m.notes.last_dream()
    m.dream(force=True)  # empty answer: error -> lock time rolls back, gate is open again
    assert m.notes.last_dream() == prior == 0.0
    m.notes.lock_path.write_text("999999", encoding="utf-8")  # as if another (live) process were holding it
    import alice.memdir as md

    real = md._pid_alive
    md._pid_alive = lambda pid: True
    try:
        assert "another process" in " ".join(m.dream(force=True))
    finally:
        md._pid_alive = real
        os.utime(m.notes.lock_path, (0, 0))


def test_failing_dream_backs_off_instead_of_retrying_every_scan():
    calls = []

    def broken(p):
        calls.append(1)
        return "bozuk cevap"

    m, _ = _dream_mem(broken, sessions=1)
    m.save_note("user-x", "x", "user", "x")
    _chat(m, time.time() - 5 * 3600, "konuşma", "cevap")
    assert m.maybe_dream() and len(calls) == 1
    m._last_dream_scan = None  # as if 10 minutes had passed
    assert not m.maybe_dream() and len(calls) == 1  # backoff after the error: model not called again
    m._dream_retry_at = 0.0  # backoff is over
    m._last_dream_scan = None
    assert m.maybe_dream() and len(calls) == 2 and m._dream_fails == 2


def test_pending_sessions_do_not_split_a_conversation_at_the_page_boundary(monkeypatch):
    m = mem(session_gap_minutes=20)
    monkeypatch.setattr(LongMemory, "PENDING_PAGE", 10)
    base = time.time() - 86400

    def turns(m, start, n):
        with m._lock:
            for i in range(n):
                m.db.execute("INSERT INTO episodes(ts, user, reply) VALUES (?, ?, 'tamam')", (start + i * 60, f"tur {i}"))
            m.db.commit()

    turns(m, base, 4)                # 1st conversation: 4 turns
    turns(m, base + 3600, 9)         # 2nd conversation: 9 turns, cut off in the middle of the page (10)
    groups = m.pending_sessions()
    assert [len(g) for g in groups] == [4]  # the unfinished 2nd conversation is not summarized this pass
    m._meta("digested_until", repr(groups[0][-1].ts))
    assert [len(g) for g in m.pending_sessions()] == [9]  # it comes in whole on the next pass

    m2 = mem(session_gap_minutes=20)
    turns(m2, base, 15)  # a single conversation longer than a page: a part is summarized to make progress
    assert [len(g) for g in m2.pending_sessions()] == [10]


def test_learn_turn_counter_is_thread_safe(monkeypatch):
    import sys
    import threading

    m = LongMemory(":memory:", {"extract_every": 4, "digest": False}, lambda p: "{}")
    calls = []
    monkeypatch.setattr(m, "_extract", lambda cwd: calls.append(1))
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # let threads switch often
    try:
        threads = [threading.Thread(target=lambda: [m.learn("tamam", background=False) for _ in range(100)])
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert len(calls) == 800 // 4 and m._turns_since_extract == 0


def test_chat_history_off_keeps_no_turns_and_says_so():
    m = LongMemory(":memory:", {"chat_history": False})
    assert m.record("dün sınavım vardı, kötü geçti", "geçmiş olsun") is None
    assert m.history_counts() == {"episodes": 0, "summaries": 0}
    assert "is off" in m.search_chats("sınav") and "is off" in m.recent_chats()
    m.chat_history = True
    assert m.record("dün sınavım vardı, kötü geçti", "geçmiş olsun") is not None
    assert m.history_counts()["episodes"] == 1
