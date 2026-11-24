from __future__ import annotations

import time
from datetime import datetime

import httpx
import pytest

from alice.core.schedule import ScheduledTask, TaskStore
from alice.core.store import Store
from alice.extras import brief, media, notes, report


def test_notes_add_search_undo():
    t0 = datetime(2026, 10, 1, 9).timestamp()
    notes.add("Unity'de zıplama hatasına bak", ts=t0)
    notes.add("Annemi ara", ts=t0 + 86400 * 7)
    assert [n.text for n in notes.search("ZIPLAMA")] == ["Unity'de zıplama hatasına bak"]
    assert [n.text for n in notes.search(since=t0 + 3600)] == ["Annemi ara"]
    assert notes.remove_last().text == "Annemi ara" and len(notes.all_notes()) == 1


def test_weather_parsing():
    data = {"current_condition": [{"temp_C": "14", "lang_tr": [{"value": "Parçalı Bulutlu"}]}],
            "weather": [{"mintempC": "9", "maxtempC": "18", "hourly": [{"chanceofrain": "10"}, {"chanceofrain": "65"}]}]}
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)))
    out = brief.weather("Istanbul", c)
    assert out == "Istanbul: now 14°C, parçalı bulutlu; today low 9°C, high 18°C, chance of rain up to 65%"
    assert "set [brief] city" in brief.weather("")


def test_brief_gather(tmp_path):
    now = datetime(2026, 10, 12, 8, 0)  # pazartesi
    ts = TaskStore(tmp_path / "t.json")
    ts.add(ScheduledTask("0 2 * * *", "yedekle", description="gece yedeği", last_run=now.timestamp() - 6 * 3600,
                         last_result="12 proje yedeklendi"))
    st = Store(":memory:")
    st.add_event("alert", "ALICE", "Disk %96 dolu", "warn", "pc", ts=now.timestamp() - 3 * 3600)
    notes.add("yarın raporu bitir", ts=now.timestamp() - 14 * 3600)
    d = brief.gather({"city": "", "days": {"pazartesi": "okul", "salı": "staj"}}, st, ts, now=now,
                     weather_fn=lambda c: "güneşli")
    assert d["date"].endswith("Monday") and d["today"] == "okul" and d["tomorrow"] == "staj"
    assert d["tasks_overnight"] == ["gece yedeği: 12 proje yedeklendi"]
    assert "Disk %96 dolu" in d["overnight"][0] and d["left_from_yesterday"] == ["yarın raporu bitir"]
    assert "today: okul" in brief.render(d)
    t = brief.install("07:45", weekdays_only=True)
    assert t.cron == "45 7 * * 1-5" and TaskStore().get("brief")


def test_weekly_report(tmp_path):
    st = Store(":memory:")
    now = time.time()
    for i in range(50):
        t = now - 6 * 86400 + i * 3600
        st.add("pc.gpu.temp", 60 + i % 20, ts=t)
        st.add("pc.disk.c.percent", 80 + i * 0.1, ts=t)
        st.add("pc.proc.unity.exe.mem_mb", 4096, ts=t)
        st.add("pc.proc.chrome.exe.mem_mb", 2048, ts=t)
    st.add("pc.gpu.temp", 55, ts=now - 10 * 86400)
    st.add_event("alert", "ALICE", "GPU 88°C", "warn", "pc", ts=now - 86400)
    r = report.build(st, 7, now)
    txt = report.text(r)
    assert "Graphics card temperature: avg" in txt and "vs. the previous week" in txt
    assert "Disk c: 85% full (+4.9 points)" in txt and txt.index("unity.exe") < txt.index("chrome.exe")
    assert "GPU 88°C ×1" in txt
    page = report.save_html(r, tmp_path)
    assert "Weekly computer report" in page.read_text(encoding="utf-8")
    assert report.install().cron == "0 20 * * 0"


def test_media_linux_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(media.sys, "platform", "linux")
    monkeypatch.setattr(media, "_run", lambda cmd: calls.append(cmd) or True)
    monkeypatch.setattr(media.shutil, "which", lambda n: "/usr/bin/" + n)
    media.key("next")
    media.key("volume_up", 5)
    media.set_volume(40)
    media.shutdown_in(30)
    media.cancel_shutdown()
    assert calls == [["playerctl", "next"], ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"],
                     ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "40%"], ["shutdown", "-h", "+30"],
                     ["shutdown", "-c"]]
    with pytest.raises(ValueError):
        media.key("yok")


def test_media_tool_risk():
    from alice.tools.extras import Media

    assert Media().risk_for({"action": "shutdown_in"}) == "danger"
    assert Media().risk_for({"action": "next"}) == "computer"


def test_report_tool(tmp_path):
    from alice.config import Config, default_config_dict
    from alice.tools import ToolContext
    from alice.tools.extras import Report

    ctx = ToolContext(cwd=tmp_path, config=Config(default_config_dict()))
    ctx.state["core_store"] = Store(":memory:")
    assert "today:" in Report().run(ctx, kind="morning").text
    assert "Rapor:" in Report().run(ctx, kind="pc").text


def test_weather_tool_uses_config_city(monkeypatch):
    from alice.extras import brief
    from alice.tools.extras import Weather

    seen = {}
    monkeypatch.setattr(brief, "weather", lambda city, auto=False: seen.update(city=city, auto=auto) or "ok")

    class Ctx:
        class config:
            data = {"brief": {"city": "Ankara"}}

    assert Weather().run(Ctx(), "").text == "ok"
    assert seen == {"city": "Ankara", "auto": True}
