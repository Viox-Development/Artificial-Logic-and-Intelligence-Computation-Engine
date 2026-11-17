from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from alice import plugins, selftools
from alice.tools.base import DANGER, EXEC, READ, WRITE, ToolError
from alice.tools.selftool import MakeTool

GOOD = '''
from alice.plugins import Plugin
from alice.tools.base import READ, Tool, ToolContext, ToolResult, obj


class Recent(Tool):
    name = "steam_recent_list"
    description = "Son oynanan oyunları listeler."
    parameters = obj({"n": {"type": "integer", "description": "Kaç oyun, ör. 3."}}, [])
    risk = READ

    def run(self, ctx: ToolContext, n: int = 3) -> ToolResult:
        games = fetch()[:n]
        if not games:
            raise RuntimeError("liste boş")
        return ToolResult(", ".join(games))


def fetch():
    return ["War Thunder", "DCS World", "Factorio", "Portal 2"]


PLUGIN = Plugin(name="steam_recent", tools=[Recent()], description="Steam son oyunlar", keywords=r"steam")
'''

GOOD_TEST = '''
import steam_recent


def test_lists_games():
    out = steam_recent.Recent().run(None, n=2)
    assert out.text == "War Thunder, DCS World"
'''

NO_NET = {"network": [], "folders": [], "apps": [], "asks_each_time": False}


@pytest.fixture(autouse=True)
def clean_pending():
    selftools._PENDING.clear()
    yield
    selftools._PENDING.clear()


# ------------------------------------------------------------------ manifest
def test_good_code_passes_manifest_check():
    assert selftools.check_code(GOOD, "steam_recent", NO_NET) == []


@pytest.mark.parametrize("code, manifest, problem", [
    ("import httpx\nr = httpx.get('https://api.steampowered.com/x')\nPLUGIN = 1", NO_NET, "network boş"),
    ("import httpx\nr = httpx.get('https://evil.example/x')\nPLUGIN = 1",
     {**NO_NET, "network": ["api.steampowered.com"]}, "bildirgede olmayan adres: evil.example"),
    ("import subprocess\nsubprocess.run(['notepad'])\nPLUGIN = 1", NO_NET, "apps boş"),
    ("from pathlib import Path\nPath('x.txt').write_text('a')\nPLUGIN = 1", NO_NET, "folders boş"),
    ("open('x.txt', 'w')\nPLUGIN = 1", NO_NET, "folders boş"),
    ("p = 'C:/Users/kresu/Documents/gizli.txt'\nPLUGIN = 1", {**NO_NET, "folders": ["C:/Users/kresu/Music"]},
     "bildirgede olmayan klasör"),
    ("eval('1+1')\nPLUGIN = 1", NO_NET, "eval() kullanılamaz"),
    ("import ctypes\nPLUGIN = 1", NO_NET, "ctypes modülü kullanılamaz"),
    ("x = 1", NO_NET, "PLUGIN"),
    ("class A:\n    name = 'baska_arac'\nPLUGIN = 1", NO_NET, "'steam_recent_' ile başlamalı"),
    ("def f(:\n", NO_NET, "sözdizimi"),
])
def test_manifest_violations_are_rejected(code, manifest, problem):
    probs = selftools.check_code(code, "steam_recent", selftools.normalize_manifest(manifest))
    assert any(problem in p for p in probs), probs


def test_declared_access_is_allowed():
    code = ("import httpx\nfrom pathlib import Path\nr = httpx.get('https://store.steampowered.com/a')\n"
            "Path('C:/Users/kresu/Music/a.txt').write_text('x')\nPLUGIN = 1")
    m = selftools.normalize_manifest({"network": ["steampowered.com"], "folders": ["C:/Users/kresu/Music"]})
    assert selftools.check_code(code, "steam_recent", m) == []
    assert selftools.describe_access(m) == "internet: steampowered.com; klasör: C:/Users/kresu/Music"
    assert selftools.describe_access(selftools.normalize_manifest({})) == "hiçbir şeye erişmez (sadece hesap yapar)"


# ---------------------------------------------------------------- flow
def test_prepare_rejects_before_sandbox(monkeypatch):
    monkeypatch.setattr(selftools, "run_tests", lambda *a, **k: pytest.fail("denetimi geçmeyen kod denenmemeli"))
    ok, msg = selftools.prepare("steam_recent", "son oyunlar", "listeler", {}, "import httpx\nPLUGIN = 1", "t", None)
    assert not ok and "network boş" in msg
    ok, msg = selftools.prepare("Steam-Recent", "", "x", {}, GOOD, "t", None)
    assert not ok and "küçük harf" in msg
    ok, msg = selftools.prepare("flight", "", "x", {}, GOOD, "t", None, existing={"flight"})
    assert not ok and "zaten var" in msg
    assert selftools._PENDING == {}


def test_failed_test_means_no_pending(monkeypatch):
    monkeypatch.setattr(selftools, "run_tests", lambda *a, **k: (False, "1 failed"))
    ok, msg = selftools.prepare("steam_recent", "son oyunlar", "listeler", NO_NET, GOOD, GOOD_TEST, None)
    assert not ok and "geçmedi" in msg and selftools._PENDING == {}
    assert not (plugins.plugin_dir() / "steam_recent.py").exists()


def test_install_only_after_prepare_then_usable_and_tracked(monkeypatch):
    monkeypatch.setattr(selftools, "run_tests", lambda *a, **k: (True, "1 passed"))
    with pytest.raises(ValueError):
        selftools.install("steam_recent")  # no pending approval: no file is written
    assert not (plugins.plugin_dir() / "steam_recent.py").exists()
    ok, msg = selftools.prepare("steam_recent", "Steam'de son oynadıklarım", "Steam'de son oynanan oyunları "
                                "listeler.", NO_NET, GOOD, GOOD_TEST, None)
    assert ok and "make_tool action=install name=steam_recent" in msg
    assert "hiçbir şeye erişmez" in selftools.approval_text("steam_recent")
    path, tools = selftools.install("steam_recent")
    assert path.read_text(encoding="utf-8").startswith('"""ALICE\'in kendi yazdığı araç: steam_recent')
    assert [t.name for t in tools] == ["steam_recent_list"]
    assert tools[0].run(None, n=1).text == "War Thunder"
    reg = selftools.load_registry()["steam_recent"]
    assert reg["uses"] == 1 and reg["errors"] == 0 and reg["need"] == "Steam'de son oynadıklarım"
    assert "steam_recent" in selftools.listing() and "Kullanım 1" in selftools.listing()


def test_three_errors_disable_tool_and_loader_skips_it(monkeypatch):
    monkeypatch.setattr(selftools, "run_tests", lambda *a, **k: (True, "ok"))
    selftools.prepare("steam_recent", "x", "listeler", NO_NET, GOOD, GOOD_TEST, None)
    _, tools = selftools.install("steam_recent")
    for _ in range(3):
        with pytest.raises(RuntimeError):
            tools[0].run(None, n=0)
    info = selftools.load_registry()["steam_recent"]
    assert info["errors"] == 3 and info["disabled"] and "liste boş" in info["last_error"]
    assert "kapalı (3 hata)" in selftools.listing()
    with pytest.raises(ToolError, match="kapatıldı"):  # it no longer runs in the same session either
        tools[0].run(None, n=1)
    assert selftools.load_registry()["steam_recent"]["errors"] == 3
    loaded = plugins.load_plugins(None)
    assert "steam_recent" not in [p.name for p in loaded]


def test_asks_each_time_makes_every_use_need_approval(monkeypatch):
    monkeypatch.setattr(selftools, "run_tests", lambda *a, **k: (True, "ok"))
    selftools.prepare("steam_recent", "x", "listeler", {**NO_NET, "asks_each_time": True}, GOOD, GOOD_TEST, None)
    _, tools = selftools.install("steam_recent")
    assert tools[0].risk_for({}) == DANGER


def test_remove_moves_file_aside(monkeypatch):
    monkeypatch.setattr(selftools, "run_tests", lambda *a, **k: (True, "ok"))
    selftools.prepare("steam_recent", "x", "listeler", NO_NET, GOOD, GOOD_TEST, None)
    path, tools = selftools.install("steam_recent")
    assert "kaldırıldı" in selftools.remove("steam_recent")
    assert not path.exists() and list((plugins.plugin_dir() / "_kaldirilan").glob("steam_recent-*.py"))
    assert selftools.load_registry()["steam_recent"]["disabled"]
    with pytest.raises(ToolError, match="kaldırıldı"):  # the copy in the running agent stops too
        tools[0].run(None, n=1)


# ---------------------------------------------------------------- tool
def test_make_tool_risk_and_approval_summary(monkeypatch):
    t = MakeTool()
    assert t.risk_for({"action": "list"}) == READ and t.risk_for({"action": "test"}) == EXEC
    assert t.risk_for({"action": "install"}) == DANGER and t.risk_for({"action": "remove"}) == WRITE
    monkeypatch.setattr(selftools, "run_tests", lambda *a, **k: (True, "ok"))
    selftools.prepare("steam_recent", "x", "Son oyunları listeler.", NO_NET, GOOD, GOOD_TEST, None)
    assert t.summary({"action": "install", "name": "steam_recent"}).startswith("'steam_recent' aracını ekleyeyim mi?")


def test_make_tool_install_adds_tool_to_running_agent(monkeypatch):
    monkeypatch.setattr(selftools, "run_tests", lambda *a, **k: (True, "ok"))
    agent = SimpleNamespace(tools={"read_file": object()})
    ctx = SimpleNamespace(state={"agent": agent})
    t = MakeTool()
    assert not t.run(ctx, action="test", name="steam_recent", need="x", description="listeler", manifest=NO_NET,
                     code=GOOD, test=GOOD_TEST).is_error
    res = t.run(ctx, action="install", name="steam_recent")
    assert "steam_recent_list" in res.text and "steam_recent_list" in agent.tools


def test_make_tool_is_in_a_group():
    from alice.tools.groups import GROUPS

    assert "make_tool" in GROUPS["selftools"][0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows düşük bütünlük kum havuzu")
def test_real_sandbox_runs_test_and_blocks_writes_outside(tmp_path):
    """End to end: the test really runs at low integrity level and cannot write to the home folder."""
    ctx = SimpleNamespace(config=SimpleNamespace(agent={}))
    ok, out = selftools.run_tests("steam_recent", selftools.file_text("steam_recent", GOOD, NO_NET, "x"), GOOD_TEST,
                                  ctx)
    assert ok, out
    target = Path.home() / "alice-selftools-yazma-denemesi.txt"
    escape = (f"import pytest\nfrom pathlib import Path\n\ndef test_escape():\n"
              f"    with pytest.raises(PermissionError):\n        Path(r'{target}').write_text('x')\n")
    ok, out = selftools.run_tests("steam_recent", selftools.file_text("steam_recent", GOOD, NO_NET, "x"), escape, ctx)
    assert ok, out
    assert not target.exists()


def test_future_import_stays_first_and_double_backslash_paths_match():
    """Live record (Steam tool): the header pushed "from __future__" to the second line and raised a
    SyntaxError; a folder written with a doubled backslash in the code did not match the manifest."""
    import ast

    from alice import selftools

    src = selftools.file_text("steam_x", "from __future__ import annotations\nX: int = 1\n", {"folders": []}, "n")
    ast.parse(src)  # must not raise SyntaxError
    compile(src, "steam_x.py", "exec")
    assert src.index("from __future__") < src.index("MANIFEST")
    folder = "C:" + "\\" + "Program Files (x86)" + "\\" + "Steam"
    doubled = folder.replace("\\", "\\\\") + "\\steamapps"
    assert selftools._under(doubled, [folder]) and selftools._under(folder.lower() + "/x", [folder])
    assert not selftools._under("C:/Program Files (x86)/SteamX", [folder])
