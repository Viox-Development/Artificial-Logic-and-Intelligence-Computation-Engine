"""Deleting a chat also removes it from memory; a new chat is not mistaken for a continuation of the old one."""

from __future__ import annotations

import time

from alice import session as sess
from alice.memory import LongMemory
from tests.conftest import tool_call  # noqa: F401 - for the app fixture
from tests.test_app import app, collect  # noqa: F401 - fixture


def _age(m: LongMemory, seconds: float) -> None:
    with m._lock:
        m.db.execute("UPDATE episodes SET ts = ts - ?", (seconds,))
        m.db.commit()


def test_forget_session_by_id_and_by_legacy_text():
    m = LongMemory(":memory:", {})
    a = m.record("rastgele bir şey yazıyorum burada", "tamam", "yazı", sid="s1")
    b = m.record("başka sohbetteki önemli konu", "peki", "yazı", sid="s2")
    old = m.record("kimliksiz eski tur metni burada", "evet", "yazı")  # record from before the sid column
    other_old = m.record("kimliksiz ama başka sohbetin turu", "hı", "yazı")
    now = time.time()
    with m._lock:
        m.db.execute("INSERT INTO summaries(start, end, channel, text) VALUES (?, ?, '', 'silinecek sohbetin özeti')",
                     (now - 5, now + 5))
        m.db.commit()
    got = m.forget_session("s1", ["kimliksiz eski tur metni burada [şu an: 15 Kas 03:00]"], now - 60, now + 60)
    left = {e.id for e in m.recent(10)}
    assert a not in left and old not in left
    assert b in left and other_old in left  # turns whose text does not match, and turns with another id, remain
    assert got["episodes"] == 2
    # There are still turns from another chat in the summary's range: the summary is not deleted.
    assert got["summaries"] == 0 and m.summaries(5)


def test_forget_session_drops_summary_when_nothing_left():
    m = LongMemory(":memory:", {})
    m.record("tek başına bir sohbet turu yazdım", "cevap", "yazı", sid="x")
    now = time.time()
    with m._lock:
        m.db.execute("INSERT INTO summaries(start, end, channel, text) VALUES (?, ?, '', 'o sohbetin özeti')",
                     (now - 5, now + 5))
        m.db.commit()
    assert m.forget_session("x") == {"episodes": 1, "summaries": 1}
    assert m.summaries(5) == [] and m.recent(5) == []


def test_profile_keeps_old_turns_out_and_marks_chat_as_new():
    m = LongMemory(":memory:", {})
    m.record("dünkü konuşmada küfürlü bir cümle kurdum", "anladım", "yazı")
    _age(m, 86400)
    prof = m.profile()
    assert "küfürlü" not in prof  # yesterday's last turn is not carried into the new chat as "kaldığımız yer"
    assert "NEW, separate conversation" in prof
    m.record("az önce sesle sorduğum soru buydu", "cevap", "ses")
    # Even recent turns of other chats stay out of the prompt now; the model looks them up when needed.
    assert "az önce sesle" not in m.profile() and "recent_chats" in m.profile()
    assert "az önce sesle" in m.recent_chats(1)


def test_deleting_open_chat_removes_memory_and_is_not_saved_again(app):  # noqa: F811
    state, srv, client, prov = app
    if state.agent.memory is None:
        state.agent.memory = LongMemory(":memory:", {})
    prov.script.append("selam, buradayım")
    events, t = collect(state, lambda e: e["type"] == "status")
    assert client.post("/api/send", json={"text": "silinecek sohbette yazdığım mesaj"}).json()["ok"]
    t.join()
    sid = state.chat.sid
    assert (sess.sessions_dir() / f"{sid}.json").exists()
    assert any(e.user.startswith("silinecek sohbette") for e in state.agent.memory.recent(5))
    r = client.post("/api/session/delete", json={"id": sid}).json()
    assert r["ok"] and r["forgot"]["episodes"] == 1
    assert not any(e.user.startswith("silinecek sohbette") for e in state.agent.memory.recent(5))
    state.chat.save()  # still open in memory: must not write the file back on close or on the next save
    assert not (sess.sessions_dir() / f"{sid}.json").exists()
    assert sid not in sess.load_meta()


def test_memory_tab_lists_and_forgets_history(app):  # noqa: F811
    state, srv, client, prov = app
    m = state.agent.memory = LongMemory(":memory:", {})
    e1 = m.record("hafıza sekmesinden silinecek tur", "tamam", "yazı")
    m.record("kalacak olan ikinci tur burada", "peki", "yazı")
    view = client.get("/api/memory").json()
    # Turns are no longer listed on the Memory page: only a count, a clear button and the on/off switch.
    assert "episodes" not in view and view["history"]["episodes"] == 2 and view["chat_history"] is True
    assert client.post("/api/memory/action", json={"action": "forget_episode", "id": e1}).json()["ok"]
    assert e1 not in {e.id for e in m.recent(10)}
    r = client.post("/api/memory/action", json={"action": "forget_history"}).json()
    assert r["ok"] and r["episodes"] == 1 and m.recent(10) == []
    assert client.post("/api/memory/action", json={"action": "chat_history", "on": False}).json() == {"ok": True, "on": False}
    assert m.chat_history is False and m.record("artık saklanmayacak tur", "tamam") is None


def test_recall_skips_model_for_small_talk_and_notes_already_in_prompt():
    asked = []

    def chooser(prompt):
        asked.append(prompt)
        return '{"selected": ["genel:user-oyun"]}'

    m = LongMemory(":memory:", {}, chooser)
    m.save_note("user-oyun", "Kullanıcı War Thunder oynar", "user", "War Thunder'da uçak ağacını açıyor, jetleri sever.")
    assert m.recall("selam naber") == "" and asked == []  # no common words with the notes: the model is not called
    assert "War Thunder" in m.recall("war thunder'da hangi uçağı açayım") and len(asked) == 1
    m.profile()  # the whole user note is now in the system prompt
    asked.clear()
    assert m.recall("war thunder'da jet seçimi") == ""  # a note already in the prompt is not added to the message again


def test_deleted_note_is_not_recreated_by_extraction_or_dream_until_user_saves_it_again():
    m = LongMemory(":memory:", {})
    m.save_note("user-sosyal-medya", "Kullanıcı YouTube ve sosyal medyada çok vakit geçirir", "user",
                "Kullanıcı YouTube ve diğer sosyal medya sitelerinde çok vakit geçirir.", source="otomatik")
    assert m.delete_note("genel:user-sosyal-medya") is not None
    again = {"save": [{"name": "user-youtube", "type": "user", "description": "Kullanıcı YouTube'da çok vakit geçirir",
                       "content": "Kullanıcı YouTube ve sosyal medya sitelerinde çok vakit geçirir."}]}
    done = m.apply_notes(again, source="dream")
    assert m.notes.scan() == [] and "the user had deleted it" in done[0]
    other = {"save": [{"name": "user-kahve", "type": "user", "description": "Kullanıcı kahveyi şekersiz içer",
                       "content": "Kullanıcı kahveyi şekersiz içer."}]}
    assert m.apply_notes(other, source="otomatik") and len(m.notes.scan()) == 1  # unrelated note is not blocked
    # If the user explicitly has the same thing saved again, the trace is cleared.
    m.remember("Kullanıcı YouTube ve sosyal medya sitelerinde çok vakit geçirir.", "fact", source="kullanıcı")
    assert m.forgotten() == []


def test_unnamed_model_save_is_checked_against_its_own_name_not_a_note_called_not():
    m = LongMemory(":memory:", {})
    m.save_note("not", "Kullanıcının elle yazdığı not", "user", "Elle yazılmış not.", source="kullanıcı")
    m.save_note("user-sosyal-medya", "Kullanıcı YouTube ve sosyal medyada çok vakit geçirir", "user",
                "Kullanıcı YouTube ve diğer sosyal medya sitelerinde çok vakit geçirir.", source="otomatik")
    m.delete_note("genel:user-sosyal-medya")
    # Model saves without a name: still check against deleted notes; a note called "not" must not be skipped.
    again = {"save": [{"type": "user", "title": "YouTube alışkanlığı",
                       "description": "Kullanıcı YouTube'da çok vakit geçirir",
                       "content": "Kullanıcı YouTube ve sosyal medya sitelerinde çok vakit geçirir."}]}
    done = m.apply_notes(again, source="otomatik")
    assert "the user had deleted it" in done[0]
    assert [n.name for n in m.notes.scan()] == ["not"]
    fresh = {"save": [{"type": "user", "title": "Kahve", "description": "Kullanıcı kahveyi şekersiz içer",
                       "content": "Kullanıcı kahveyi şekersiz içer."}]}
    m.apply_notes(fresh, source="otomatik")
    assert m.notes.get("kahve").source == "otomatik"  # source not taken from the note named "not" ("kullanıcı")
