from __future__ import annotations

from alice.core.store import Store
from alice.core.watchdogs import GameHealth, NetWatch, outages


def test_game_health_waits_two_minutes_names_memory_hog_and_rearms():
    st = Store(":memory:")
    sent = []
    g = GameHealth(st, lambda n: sent.append(n.body) or True)
    t = 1000.0
    st.add("pc.proc.discord.exe.mem_mb", 1843.0, ts=t + 100)
    st.add("pc.proc.aces.exe.mem_mb", 6000.0, ts=t + 100)  # the game itself is never suggested
    for dt in (0, 60, 121):
        st.add("pc.mem.percent", 96.0, ts=t + dt)
        g.check("flight", t + dt)
    assert sent == ["RAM yüzde 96 dolu, oyun takılabilir. En çok Discord kullanıyor (1.8 GB), kapatabilirsin."]
    st.add("pc.mem.percent", 96.0, ts=t + 400)
    g.check("flight", t + 400)
    assert len(sent) == 1  # no repeat before 30 min have passed
    st.add("pc.mem.percent", 80.0, ts=t + 460)
    g.check("flight", t + 460)  # dropped: re-armed, but silent again until 10 min pass
    for dt in (500, 621):
        st.add("pc.mem.percent", 97.0, ts=t + dt)
        g.check("flight", t + dt)
    assert len(sent) == 1
    for dt in (700, 821):
        st.add("pc.mem.percent", 97.0, ts=t + dt)
        g.check("flight", t + dt)
    assert len(sent) == 2
    st.add("pc.gpu.temp", 95.0, ts=t + 900)
    assert g.check("normal", t + 900) == [] and g.check("normal", t + 1030) == []  # silent outside gaming


def test_game_health_quiet_at_normal_gaming_gpu_temps():
    st = Store(":memory:")
    sent = []
    g = GameHealth(st, lambda n: sent.append(n.body) or True)
    for dt in range(0, 3600, 15):  # during gaming the card sits at 85-90 degrees for hours: not critical
        st.add("pc.gpu.temp", 85.0 + dt % 6, ts=1000.0 + dt)
        g.check("flight", 1000.0 + dt)
    assert sent == []
    for dt in (3600, 3700, 3730):
        st.add("pc.gpu.temp", 94.0, ts=1000.0 + dt)
        g.check("flight", 1000.0 + dt)
    assert sent == ["Ekran kartı 94 derece, kritik: kart kendini yavaşlatmaya başlar."]


def test_net_watch_announces_drop_and_return():
    st = Store(":memory:")
    sent = []
    clock = [0.0]
    results = iter([20.0, None, None, None, None, 25.0])
    w = NetWatch(st, lambda n: sent.append(n.title) or True, probe=lambda: next(results), clock=lambda: clock[0],
                 background=False)
    rows = []
    for t in (0, 30, 60, 90, 120, 150):
        clock[0] = t
        rows += w.collect()
    assert sent == ["İnternet koptu", "İnternet geri geldi"]
    assert ("net.online", 0.0, None) in rows and ("net.latency_ms", 25.0, None) in rows
    assert outages(st, 0) == (1, 2.0)


def test_short_blip_is_silent():
    st = Store(":memory:")
    sent = []
    clock = [0.0]
    results = iter([None, 20.0])
    w = NetWatch(st, lambda n: sent.append(n.title) or True, probe=lambda: next(results), clock=lambda: clock[0],
                 background=False)
    for t in (0, 30):
        clock[0] = t
        w.collect()
    assert sent == [] and outages(st, 0) == (0, 0.0)
