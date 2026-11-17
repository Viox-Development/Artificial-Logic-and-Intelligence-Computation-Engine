"""Turn-end box (next_step), persistent file cards and file preview."""

from __future__ import annotations

import base64

from alice.app.server import display_history, file_preview
from alice.tools.meta import NextStep, SENT_FILES
from tests.conftest import tool_call
from tests.test_agent import make_agent


def test_next_step_card_is_kept_in_history(workdir):
    script = [tool_call("next_step", {"recap": "Testler geçti, push edilmedi.", "question": "PR açayım mı?",
                                      "options": ["Evet", "Şimdilik kalsın", "a", "b"]}), "Bitti."]
    agent, _, _ = make_agent(script, workdir)
    agent.run_turn("işi bitir")
    tool = next(m for m in agent.messages if m.get("role") == "tool")
    assert tool["_card"]["question"] == "PR açayım mı?" and len(tool["_card"]["options"]) == 3
    items = display_history(agent.messages, agent.tools)
    card = next(i["card"] for i in items if i["type"] == "tool")
    assert card["kind"] == "next_step" and card["recap"].startswith("Testler geçti")


def test_next_step_without_question_has_no_buttons():
    r = NextStep().run(None, recap="Bitti.", options=["Evet"])
    assert r.card["options"] == [] and r.card["question"] == ""


def test_sent_files_survive_reload_and_preview(workdir):
    f = workdir / "rapor.md"
    f.write_text("# Başlık\n\nmetin", encoding="utf-8")
    agent, _, _ = make_agent([tool_call("send_file", {"paths": [str(f)]}), "gösterdim"], workdir)
    agent.run_turn("raporu göster")
    SENT_FILES.clear()  # app restarted
    items = display_history(agent.messages, agent.tools)
    assert next(i for i in items if i["type"] == "tool")["files"][0]["name"] == "rapor.md"
    assert str(f) in SENT_FILES  # still openable after history loads
    pv = file_preview(str(f), SENT_FILES)
    assert pv["ok"] and pv["kind"] == "markdown" and "# Başlık" in pv["text"]


def test_preview_refuses_files_alice_did_not_show(workdir):
    secret = workdir / "gizli.txt"
    secret.write_text("x", encoding="utf-8")
    assert not file_preview(str(secret), set())["ok"]


def test_preview_image_and_binary(workdir):
    png = workdir / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 10)
    blob = workdir / "a.bin"
    blob.write_bytes(b"\0\1\2")
    allowed = {str(png), str(blob)}
    pv = file_preview(str(png), allowed)
    assert pv["kind"] == "image" and base64.b64decode(pv["b64"]).startswith(b"\x89PNG")
    assert not file_preview(str(blob), allowed)["ok"]
