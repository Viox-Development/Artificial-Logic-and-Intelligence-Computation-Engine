from __future__ import annotations

import json
from datetime import datetime

from alice import anticipate as a


def test_update_state_and_prep_tells_setting_once():
    m = {"name": "War Thunder", "StateFlags": "6", "AutoUpdateBehavior": "1", "buildid": "42"}
    u = a.update_state(m)
    assert u["pending"] and u["on_launch_only"] and not u["downloading"]
    told, started, st = [], [], {}
    msg = a.prep_game_update(set(), lambda t, b: told.append(t), st, manifest=m, start=lambda: started.append(1) or True)
    assert "when the game is opened" in msg and told == ["War Thunder update pending"] and not started
    a.prep_game_update(set(), lambda t, b: told.append(t), st, manifest=m)
    assert len(told) == 1  # told once for the same version


def test_prep_starts_steam_silently_when_auto_update_on():
    m = {"name": "War Thunder", "StateFlags": "6", "AutoUpdateBehavior": "0", "buildid": "42"}
    started = []
    msg = a.prep_game_update(set(), lambda t, b: None, {}, manifest=m, start=lambda: started.append(1) or True)
    assert started and "in the background" in msg
    started.clear()
    a.prep_game_update({"steam.exe"}, lambda t, b: None, {}, manifest=m, start=lambda: started.append(1) or True)
    assert not started  # Steam is already open: it downloads by itself
    assert a.prep_game_update(set(), lambda t, b: None, {}, manifest={**m, "StateFlags": "4"}) == ""  # up to date


def test_steam_manifest_found_in_library(tmp_path):
    root, lib = tmp_path / "Steam", tmp_path / "Lib"
    (root / "steamapps").mkdir(parents=True)
    (lib / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "%s" } }' % str(lib).replace("\\", "\\\\"), encoding="utf-8")
    (lib / "steamapps" / "appmanifest_236390.acf").write_text(
        '"AppState" { "name" "War Thunder" "StateFlags" "4" "AutoUpdateBehavior" "0" "buildid" "7" }',
        encoding="utf-8")
    assert a.steam_manifest(root=root) == {"name": "War Thunder", "StateFlags": "4", "AutoUpdateBehavior": "0",
                                           "buildid": "7"}


def test_quiet_game_turns_dnd_on_in_flight_and_restores(tmp_path):
    state = {"v": 0}
    wnf = (lambda: state["v"], lambda v: state.update(v=v) or True)
    q = a.QuietGame(wnf=wnf, path=tmp_path / "s.json")
    assert q.mode("flight") and state["v"] == a.DND_ALARMS_ONLY
    assert q.mode("flight") == ""  # we already turned it on
    assert q.mode("normal") and state["v"] == 0
    state["v"] = 1  # the user's own "öncelikli" (priority) setting: must not be touched
    assert q.mode("flight") == "" and state["v"] == 1
    assert q.mode("normal") == "" and state["v"] == 1


def test_quiet_game_does_not_override_user_change(tmp_path):
    state = {"v": 0}
    q = a.QuietGame(wnf=(lambda: state["v"], lambda v: state.update(v=v) or True), path=tmp_path / "s.json")
    q.mode("flight")
    state["v"] = 0  # the user turned it off during the match
    assert q.mode("normal") == "" and state["v"] == 0
    assert "dnd_prev" not in json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))


def test_morning_habits_plan_and_ready_line(tmp_path):
    lines = []
    for d in range(1, 6):
        lines.append(f"2026-10-0{d} 07:{10 + d}:00 live [gemini] duyulan: Alice bugün hava nasıl? | cevap: ...")
        lines.append(f"2026-10-0{d} 07:{20 + d}:00 live [gemini] duyulan: program ne | cevap: ...")  # not the first one
    lines.append("2026-10-06 15:00:00 live [gemini] duyulan: hava | cevap: ...")  # afternoon ones don't count
    topics, starts = a.morning_habits(lines)
    assert topics == {"hava": 5} and len(starts) == 5
    wanted, at = a.morning_plan(lines)
    assert wanted == ["hava"] and at == 7 * 60 + 13 - 20
    m = a.Morning(cfg=None, log=tmp_path / "voice.log", path=tmp_path / "s.json",
                  fetch={"hava": lambda: "İstanbul 14°C, parçalı bulutlu"})
    (tmp_path / "voice.log").write_text("\n".join(lines), encoding="utf-8")
    assert m.check(datetime(2026, 10, 7, 6, 40)) is None  # too early yet
    assert m.check(datetime(2026, 10, 7, 6, 55)) == {"hava": "İstanbul 14°C, parçalı bulutlu"}
    assert m.check(datetime(2026, 10, 7, 7, 30)) is None  # once a day
    assert a.ready_line(datetime(2026, 10, 7, 8, 0), tmp_path / "s.json") == "hava: İstanbul 14°C, parçalı bulutlu"
    assert a.ready_line(datetime(2026, 10, 7, 14, 0), tmp_path / "s.json") == ""  # stale after the afternoon
    assert a.ready_line(datetime(2026, 10, 8, 8, 0), tmp_path / "s.json") == ""


def test_session_lines_use_first_user_message(tmp_path):
    (tmp_path / "20261003-071500.json").write_text(json.dumps({"messages": [
        {"role": "user", "content": "hava nasıl"}, {"role": "assistant", "content": "x"}]}), encoding="utf-8")
    (tmp_path / "bozuk.json").write_text("{", encoding="utf-8")
    assert a.session_lines(tmp_path) == ["2026-10-03 07:15:00 sohbet duyulan: hava nasıl"]


def test_game_update_prep_does_not_erase_dnd_note_written_meanwhile(tmp_path, monkeypatch):
    # If a flight starts during the pre-game prep and QuietGame writes dnd_prev, the prep's record must not erase it
    # (if erased, "Rahatsız etmeyin" (Do Not Disturb) would stay on after the flight).
    from types import SimpleNamespace

    from alice.core import service

    path = tmp_path / "anticipate.json"
    monkeypatch.setattr(a, "state_path", lambda: path)
    dnd = {"v": 0}
    q = a.QuietGame(wnf=(lambda: dnd["v"], lambda v: dnd.update(v=v) or True))

    def prep(processes, notify, st, **kw):
        notify("başlık", "gövde")  # meanwhile another thread switched to flight
        st["update_told"] = "7"
        return "güncelleme bekliyor"

    monkeypatch.setattr(a, "prep_game_update", prep)
    events = []
    core = object.__new__(service.Core)
    core._processes = lambda: set()
    core.notifier = SimpleNamespace(send=lambda n: q.mode("flight"))
    core.store = SimpleNamespace(add_event=lambda *x: events.append(x))
    core._prep_game_update()
    st = json.loads(path.read_text(encoding="utf-8"))
    assert st["update_told"] == "7" and st["dnd_prev"] == 0 and events
    assert q.mode("normal") and dnd["v"] == 0
    assert not list(tmp_path.glob("*.tmp"))


def test_morning_event_lists_prepared_answers():
    from types import SimpleNamespace

    from alice.core import service

    events = []
    core = object.__new__(service.Core)
    core.morning = SimpleNamespace(check=lambda: {"hava": "Ankara 12 °C,\n parçalı bulutlu", "posta": "x" * 300})
    core.store = SimpleNamespace(add_event=lambda *x: events.append(x))
    core._morning()
    body = events[0][2]
    assert body.startswith("hava: Ankara 12 °C, parçalı bulutlu; posta: xxx") and body.endswith("…")
    assert len(body) < 220


def test_steam_outside_windows(monkeypatch, tmp_path):
    from alice import anticipate as a

    monkeypatch.setattr(a.sys, "platform", "linux")
    monkeypatch.setattr(a, "STEAM_DIRS", (str(tmp_path / "nope"), str(tmp_path / "Steam")))
    assert a._steam_root() is None
    (tmp_path / "Steam" / "steamapps").mkdir(parents=True)
    assert a._steam_root() == tmp_path / "Steam"
    assert a.steam_running({"steam"}) and a.steam_running({"Steam.exe"}) and not a.steam_running({"steamwebhelper"})
