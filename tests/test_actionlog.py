"""Eylem kaydı: başkasına giden her şey yazılır (gönderilen, reddedilen, engellenen, başarısız)."""

from __future__ import annotations

import time

from alice import actionlog
from alice.tools.base import SEND, Tool, ToolResult
from alice.tools.core import ActionLog
from tests.test_agent import make_agent
from tests.conftest import tool_call


class Sender(Tool):
    name = "mesajci"
    description = "test: mesaj gönderir"
    parameters = {"type": "object", "properties": {"kime": {"type": "string", "description": "kişi"},
                                                   "metin": {"type": "string", "description": "mesaj"}}}
    risk = SEND

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[dict] = []

    def summary(self, args):
        return f"{args.get('kime')} ← {args.get('metin')}"

    def run(self, ctx, **args):
        if self.fail:
            return ToolResult("ağ hatası", is_error=True)
        self.sent.append(args)
        return ToolResult("gönderildi")


def agent_with(sender, workdir, approve="y", mode="auto"):
    script = [tool_call("mesajci", {"kime": "Ahmet", "metin": "geliyorum"}), {"content": "tamam"}]
    agent, _provs, _ui = make_agent(script, workdir, mode=mode, approve=approve)
    agent.tools["mesajci"] = sender
    return agent


def test_gonderilen_kaydedilir(workdir):
    s = Sender()
    agent_with(s, workdir).run_turn("Ahmet'e geliyorum yaz")
    assert s.sent == [{"kime": "Ahmet", "metin": "geliyorum"}]
    row = actionlog.recent(1)[0]
    assert row["outcome"] == "sent" and row["tool"] == "mesajci" and "Ahmet ← geliyorum" in row["summary"]
    assert '"metin": "geliyorum"' in row["args"]


def test_reddedilen_de_kaydedilir(workdir):
    s = Sender()
    agent_with(s, workdir, approve="n").run_turn("Ahmet'e yaz")
    assert s.sent == [] and actionlog.recent(1)[0]["outcome"] == "rejected"


def test_plan_modunda_engellenen_kaydedilir(workdir):
    s = Sender()
    agent_with(s, workdir, mode="plan").run_turn("Ahmet'e yaz")
    assert s.sent == [] and actionlog.recent(1)[0]["outcome"] == "blocked"


def test_basarisiz_kaydedilir_nedeniyle(workdir):
    agent_with(Sender(fail=True), workdir).run_turn("yaz")
    row = actionlog.recent(1)[0]
    assert row["outcome"] == "failed" and "ağ hatası" in row["detail"]


def test_sadece_disa_donuk_eylemler_kaydedilir(workdir):
    script = [tool_call("write_file", {"path": "a.txt", "content": "x"}), {"content": "tamam"}]
    agent, _p, _u = make_agent(script, workdir)
    agent.run_turn("dosya yaz")
    assert actionlog.recent() == []


def test_arac_ve_bicim(workdir):
    actionlog.log("discord", "discord · send content=selam", "sent", {"content": "selam"})
    actionlog.log("whatsapp", "app whatsapp · send", "rejected")
    out = ActionLog().run(None, limit=5).text
    lines = out.splitlines()
    assert "[reddedildi] app whatsapp · send" in lines[0] and "[gönderildi] discord" in lines[1]
    assert ActionLog().run(None, hours=0.0001).text  # saat sınırı
    old = time.time() - 3 * 3600
    assert all(float(r["ts"]) >= old for r in actionlog.recent(10, since=old))


def test_bos_kayit(workdir):
    assert ActionLog().run(None).text.startswith("kayıt yok")


def test_buyuyunce_doner(monkeypatch):
    monkeypatch.setattr(actionlog, "MAX_BYTES", 300)
    for i in range(20):
        actionlog.log("t", f"mesaj {i}", "sent", {"i": i})
    assert actionlog.path().with_suffix(".jsonl.1").exists()
    assert actionlog.recent(1)[0]["summary"] == "mesaj 19"
