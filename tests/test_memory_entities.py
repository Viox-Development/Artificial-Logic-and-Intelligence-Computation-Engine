"""Categories, nicknames, [[links]] and chat context in recall (alice/memdir.py, alice/memory.py)."""

from __future__ import annotations

import json

from alice.memdir import NoteBook, linked_notes
from alice.memory import LongMemory
from alice.tools import ToolContext
from alice.tools.meta import Memory


def _people(m: LongMemory) -> None:
    m.save_note("efe", 'Efe ("gravity") — Discord grubundan yakın arkadaş; read when Efe or gravity comes up', "",
                "Aynı arkadaş grubundalar: [[nehir]].", category="people", aliases=["gravity"])
    m.save_note("nehir", "Nehir — yakın online arkadaş; read when Nehir comes up", "", "Discord'da tanıştılar.",
                category="people")
    m.save_note("staj", "12. sınıf stajı; read when staj or işletme comes up", "", "Uzun yol.", category="areas")


def test_notes_go_to_category_folders_and_the_index_groups_them(tmp_path):
    b = NoteBook(tmp_path / "m")
    b.save("efe", "Efe — arkadaş", "", "x", category="people")
    b.save("profile", "Kim olduğu", "", "y")
    b.save("staj", "Staj", "", "z", category="areas")
    assert (b.root / "people" / "efe.md").is_file() and (b.root / "profile.md").is_file()
    assert {n.name: n.category for n in b.scan()} == {"efe": "people", "profile": "you", "staj": "areas"}
    idx = b.index_raw()
    assert idx.index("## You") < idx.index("## Areas") < idx.index("## People")
    assert "](people/efe.md)" in idx
    # a different category moves the file; an empty one leaves it where it is
    b.save("efe", "Efe — arkadaş", "", "x2", category="topics")
    assert not (b.root / "people" / "efe.md").exists() and (b.root / "topics" / "efe.md").is_file()
    b.save("efe", "Efe — arkadaş", "", "x3")
    assert (b.root / "topics" / "efe.md").is_file()


def test_nickname_and_link_resolve_without_a_model():
    m = LongMemory(":memory:", {})
    _people(m)
    notes = m.notes.scan()
    assert m.notes.resolve_alias("gravity").name == "efe"
    got = [n.name for n in linked_notes("gravity bugün ne dedi biliyor musun", notes)]
    assert got[:2] == ["efe", "nehir"]  # direct mention, then the note it links to
    assert [n.name for n in linked_notes("işletmede uzun bir gün", notes)] == ["staj"]
    assert linked_notes("selam naber", notes) == []


def test_recall_uses_the_recent_turns_of_this_chat():
    m = LongMemory(":memory:", {})
    _people(m)
    assert "Nehir" not in m.recall("ona ne yazsam bilmiyorum")
    hit = m.recall("ona ne yazsam bilmiyorum", context="Kullanıcı: Nehir bugün çok sessizdi\nALICE: Belki yorgundur.")
    assert "Discord'da tanıştılar" in hit


def test_agent_passes_chat_context_to_recall(workdir):
    from tests.test_agent import make_agent

    agent, _, _ = make_agent([], workdir)
    agent.messages += [{"role": "user", "content": "Nehir bugün çok sessizdi"},
                       {"role": "assistant", "content": "Belki yorgundur."},
                       {"role": "user", "content": "dürt", "_nudge": True}]
    ctx = agent._recent_chat_text()
    assert "Nehir bugün" in ctx and "Belki yorgundur" in ctx and "dürt" not in ctx


def test_model_answers_with_category_and_no_type_are_saved():
    m = LongMemory(":memory:", {})
    done = m.apply_notes({"save": [{"name": "mitsuki", "category": "people", "aliases": ["mitsu"],
                                    "description": "Mitsuki — ŞΣY sunucusundan arkadaş", "content": "Sunucudan."}]},
                         source="otomatik")
    assert done == ["added genel:mitsuki"]
    n = m.notes.get("mitsuki")
    assert n.category == "people" and "mitsu" in n.aliases


def test_memory_tool_saves_category_and_reads_by_nickname(workdir):
    from alice.memory import attach_memory
    from tests.test_agent import make_agent

    agent, _, _ = make_agent([], workdir)
    attach_memory(agent, "yazı")
    ctx: ToolContext = agent.ctx
    tool = Memory()
    r = tool.run(ctx, "save", name="efe", category="people", aliases="gravity, efo",
                 description='Efe ("gravity") — arkadaş', content="Grup: [[nehir]].")
    assert "people/efe.md" in r.text
    r = tool.run(ctx, "read", name="gravity")
    assert "Grup:" in r.text and "[[nehir]]" in r.text
    m = agent.memory
    assert json.dumps(list(m.notes.get("efe").alias_list), ensure_ascii=False) == '["gravity", "efo"]'
