"""DCS wingmen: Turkish utterance -> command, UDP sending, hook installation and the hook's security boundaries.
The Lua side cannot run without DCS; only static checks live here (syntax too, if luac is available)."""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import time

import pytest

from alice.sims import dcs
from alice.sims import wingmen as wm
from alice.sims.wingmen import Command, parse
from alice.tools.base import ToolContext, ToolError
from alice.tools.flight import DcsWingman
from tests.conftest import make_config


@pytest.mark.parametrize("text, verb, index, target, direction", [
    ("İkinci, sağdaki SAM'i bastır", "attack", 2, "sam", "right"),
    ("ikinci sağdaki sam'ı bastır", "attack", 2, "sam", "right"),
    ("Üçüncü, soldaki hava savunmasını yok et", "attack", 3, "sam", "left"),
    ("Dördüncü öndeki uçaksavara saldır", "attack", 4, "sam", "front"),
    ("Birinci, arkadaki düşman uçağına saldır", "attack", 1, "air", "behind"),
    ("İkinci bandit'i vur", "attack", 2, "air", "any"),
    ("2 numara sağdaki tankları vur", "attack", 2, "ground", "right"),
    ("Üç, öndeki zırhlı konvoya saldır", "attack", 3, "ground", "front"),
    ("İkinci soldaki gemiyi vur", "attack", 2, "ship", "left"),
    ("Kanat adamı saldır", "attack", 2, "any", "any"),
    ("Herkes sağdaki SAM'i bastırsın", "attack", 0, "sam", "right"),
    ("Bir SAM'i vur", "attack", 1, "sam", "any"),
    ("Sağdaki bir SAM'i vur ikinci", "attack", 2, "sam", "right"),
    ("İkinci, yanıma gel", "rejoin", 2, "any", "any"),
    ("Üçüncü formasyona katıl", "rejoin", 3, "any", "any"),
    ("hepsi toplanın", "rejoin", 0, "any", "any"),
    ("İkinci, bulunduğun yerde bekle", "hold", 2, "any", "any"),
    ("Üçüncü tur at", "hold", 3, "any", "any"),
    ("İkinci eve dön", "rtb", 2, "any", "any"),
    ("Dördüncü, üsse dön", "rtb", 4, "any", "any"),
    ("üçüncü geri dön", "rtb", 3, "any", "any"),
    ("3. üsse dön", "rtb", 3, "any", "any"),
])
def test_parse_turkish(text, verb, index, target, direction):
    c = parse(text)
    assert c is not None, text
    assert (c.verb, c.index, c.target, c.direction) == (verb, index, target, direction)


@pytest.mark.parametrize("text", ["", "merhaba", "bugün hava nasıl", "ikinci şarkıyı aç"])
def test_parse_returns_none_without_verb(text):
    assert parse(text) is None


def test_eve_don_is_not_front_direction():
    # the "ön" inside "dön" must not count as a direction
    assert parse("ikinci eve dön").direction == "any"


def test_command_line_and_validation():
    assert Command("attack", 2, "sam", "right").line() == "ALICE1|attack|2|sam|right|"
    assert Command("rejoin", 0, group="Uzi 2").line() == "ALICE1|rejoin|0|any|any|Uzi 2"
    for bad in (Command("fire"), Command("attack", 12), Command("attack", target="nuke"),
                Command("attack", direction="up"), Command("hold", group="x|y"),
                Command("hold", group="a'); os.exit() --"), Command("hold", group="a" * 41)):
        with pytest.raises(ValueError):
            bad.line()


def test_describe_is_turkish():
    assert Command("attack", 2, "sam", "right").describe() == "İkinci: sağdaki SAM -> saldır"
    assert Command("rtb", 0).describe() == "tüm kanat adamları: üsse dön"


def test_send_delivers_line_to_udp():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    try:
        assert wm.send(Command("attack", 3, "sam", "left"), port=rx.getsockname()[1])
        data, _ = rx.recvfrom(1024)
        assert data == b"ALICE1|attack|3|sam|left|"
    finally:
        rx.close()


def test_send_never_blocks_when_nobody_listens():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]  # nobody is listening after it was closed
    t = time.monotonic()
    for _ in range(20):
        wm.send(Command("hold"), port=free)
    assert time.monotonic() - t < 1.0


def test_send_returns_false_on_socket_error(monkeypatch):
    class Bad:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setblocking(self, flag):
            pass

        def sendto(self, *a):
            raise OSError("ağ yok")

    monkeypatch.setattr(wm.socket, "socket", Bad)
    assert wm.send(Command("hold")) is False


def test_settings_from_config():
    assert wm.settings({}) == (wm.PORT, [])
    assert wm.settings({"dcs_wingmen_port": "5000", "dcs_wingmen": ["Uzi 2", " ", "Uzi 3"]}) == (5000, ["Uzi 2", "Uzi 3"])
    assert wm.settings({"dcs_wingmen": "Uzi 2"}) == (wm.PORT, ["Uzi 2"])


# ------------------------------------------------------------------ tool
def test_tool_sends_parsed_command(monkeypatch, workdir):
    sent = []
    monkeypatch.setattr(wm, "send", lambda cmd, port=wm.PORT: sent.append((cmd.line(), port)) or True)
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))
    r = DcsWingman().run(ctx, command="İkinci, sağdaki SAM'i bastır")
    assert sent == [("ALICE1|attack|2|sam|right|", 41231)]
    assert "gönderildi" in r.text and "SAM" in r.text
    DcsWingman().run(ctx, verb="rtb", index="3")
    assert sent[-1][0] == "ALICE1|rtb|3|any|any|"
    with pytest.raises(ToolError):
        DcsWingman().run(ctx, command="merhaba")
    with pytest.raises(ToolError):
        DcsWingman().run(ctx, verb="attack", target="nuke")


def test_tool_is_in_flight_group():
    from alice.tools.groups import GROUPS, groups_for_text

    assert "dcs_wingman" in GROUPS["flight"][0]
    assert "flight" in groups_for_text("İkinci, sağdaki SAM'i bastır")


# ------------------------------------------------------------------ installation
def test_install_writes_hook_and_uninstall_removes(tmp_path):
    d = tmp_path / "DCS"
    d.mkdir()
    dcs.install([d], wingmen_port=45001, wingmen=['Uzi "2"', "Kılıç 3"])
    hook = d / "Scripts" / "Hooks" / "alice_wingmen_hook.lua"
    text = hook.read_text(encoding="utf-8")
    assert "local ALICE_PORT = 45001" in text
    assert 'local ALICE_NAMES = {"Uzi \\"2\\"", "Kılıç 3"}' in text
    assert dcs.ensure_installed([d], wingmen_port=45001, wingmen=['Uzi "2"', "Kılıç 3"]) == []
    assert dcs.ensure_installed([d], wingmen_port=45002, wingmen=['Uzi "2"', "Kılıç 3"]) == [d]  # port changed
    hook.write_text("eski", encoding="utf-8")
    assert dcs.ensure_installed([d], wingmen_port=45002, wingmen=['Uzi "2"', "Kılıç 3"]) == [d]  # content changed
    hook.unlink()
    assert dcs.ensure_installed([d], wingmen_port=45002, wingmen=['Uzi "2"', "Kılıç 3"]) == [d]  # deleted
    assert hook.exists()
    assert dcs.uninstall([d]) == [d]
    assert not hook.exists() and not (d / "Scripts" / "alice_export.lua").exists()


def test_hook_names_cannot_break_out_of_lua_string():
    text = dcs.hook_lua(41231, ['a"} os.execute("x") --', "b\\", "c\nd"])
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("local ALICE_NAMES"))
    assert line.strip() == r'local ALICE_NAMES = {"a\"} os.execute(\"x\") --", "b\\", "cd"}'


def test_hub_passes_wingmen_settings(monkeypatch):
    from alice.sims import hub, warthunder

    got = {}

    class Part:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            return self

        def stop(self):
            pass

    monkeypatch.setattr(dcs, "ensure_installed", lambda **kw: got.update(kw) or [])
    monkeypatch.setattr(dcs, "DCSListener", Part)
    monkeypatch.setattr(warthunder, "WarThunderPoller", Part)

    class Store:
        def add_event(self, *a):
            pass

    h = hub.SimHub({"warthunder": False, "dcs_wingmen_port": 5001, "dcs_wingmen": ["Uzi 2"]}, Store(), None)
    h.start()
    h.stop()
    assert got["wingmen_port"] == 5001 and got["wingmen"] == ["Uzi 2"]


# ------------------------------------------------------------------ hook security (static)
HOOK_TEXT = dcs.HOOK.read_text(encoding="utf-8")


def test_hook_is_packaged_next_to_export_script():
    assert dcs.HOOK.parent == dcs.SCRIPT.parent and dcs.HOOK.name == "alice_wingmen_hook.lua"


def test_hook_does_not_touch_mission_scripting_sanitize():
    assert "MissionScripting" not in HOOK_TEXT
    assert "sanitize" not in HOOK_TEXT.lower()


def test_hook_never_evaluates_network_text():
    # The hook never compiles or runs network text as Lua; only validated fields reach the mission code via %q/%d.
    for bad in ("loadstring", "load(", "dofile", "loadfile", "require(\"lfs\")", "os.execute", "io.open"):
        assert bad not in HOOK_TEXT, bad
    assert HOOK_TEXT.count("net.dostring_in") == 1
    dispatch = HOOK_TEXT[HOOK_TEXT.index("local function dispatch"):HOOK_TEXT.index("local function poll")]
    assert "data" not in dispatch and "line" not in dispatch
    assert 'string.format("ALICE_WM.run(%q, %d, %q, %q, %q, %s)"' in dispatch


def test_hook_listens_only_on_localhost_and_checks_server():
    assert 'setsockname("127.0.0.1", ALICE_PORT)' in HOOK_TEXT
    assert "0.0.0.0" not in HOOK_TEXT and "*" not in re.findall(r'setsockname\("([^"]+)"', HOOK_TEXT)
    assert 'ip == "127.0.0.1"' in HOOK_TEXT
    assert "DCS.isServer()" in HOOK_TEXT and "DCS.isMultiplayer()" in HOOK_TEXT
    assert "settimeout(0)" in HOOK_TEXT and "MIN_GAP" in HOOK_TEXT
    assert '[%w _%-]' in HOOK_TEXT


def test_hook_lua_syntax_if_luac_available(tmp_path):
    luac = shutil.which("luac") or shutil.which("luac5.1") or shutil.which("luac5.4")
    if not luac:
        pytest.skip("luac yok")
    f = tmp_path / "hook.lua"
    f.write_text(dcs.hook_lua(41231, ["Uzi 2"]), encoding="utf-8")
    r = subprocess.run([luac, "-p", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
