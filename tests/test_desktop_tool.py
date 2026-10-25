"""alice/tools/desktop.py: pencere eşleme (Türkçe harfler, yanlış pencereyi kapatmama), argüman doğrulama,
risk sınıfları ve UIA yardımcıları. pygetwindow/pywinauto/pyperclip/wmctrl sahte nesnelerle değiştirilir;
gerçek pencere ya da süreç kullanılmaz, Linux ve Windows'ta aynı çalışır."""

from __future__ import annotations

import subprocess
import sys
import time
import types
from types import SimpleNamespace

import pytest

from alice.tools import desktop
from alice.tools.base import COMPUTER, EXEC, READ, ToolContext, ToolError
from tests.conftest import make_config


class FakeWin:
    def __init__(self, title):
        self.title = title
        self.calls: list[str] = []
        self.isMinimized = False

    def __getattr__(self, name):
        if name in ("activate", "restore", "minimize", "maximize", "close"):
            return lambda: self.calls.append(name)
        raise AttributeError(name)


@pytest.fixture
def gw(monkeypatch):
    """macOS yolu (pygetwindow): Windows'a özgü ctypes öne alma çağrılmasın."""
    wins = [FakeWin("Belgeler - Dosya Gezgini"), FakeWin("İndirilenler - Dosya Gezgini"),
            FakeWin("a.txt - Not Defteri"), FakeWin("b.txt - Not Defteri"), FakeWin("Not Defteri"), FakeWin("")]
    mod = types.ModuleType("pygetwindow")
    mod.getAllWindows = lambda: wins
    mod.getAllTitles = lambda: [w.title for w in wins] + ["   "]
    mod.getActiveWindow = lambda: wins[0]
    monkeypatch.setitem(sys.modules, "pygetwindow", mod)
    monkeypatch.setattr(desktop, "IS_WIN", False)
    monkeypatch.setattr(desktop, "IS_MAC", True)
    return wins


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(desktop, "time", SimpleNamespace(sleep=lambda s: None, monotonic=time.monotonic))


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


# ------------------------------------------------------------ eşleme yardımcıları


@pytest.mark.parametrize("a,b", [("İNDİRİLENLER", "indirilenler"), ("Işık", "işik"), ("ıŞIK", "IŞIK"),
                                 ("Straße", "STRASSE")])
def test_fold_ignores_turkish_case(a, b):
    assert desktop.fold(a) == desktop.fold(b)


def test_norm_matches_turkish_dotted_capital():
    assert desktop._norm("İnternet Explorer!") == "internetexplorer"


def test_pick_window_turkish_and_preference(gw):
    # eskiden "indirilenler" ile "İndirilenler" bulunamıyordu ("İ".lower() == "i̇")
    assert desktop.pick_window(gw, "indirilenler").title.startswith("İndirilenler")
    assert desktop.pick_window(gw, "NOT DEFTERI").title == "Not Defteri"  # tam eşleşme öncelikli
    assert desktop.pick_window(gw, "a.txt").title == "a.txt - Not Defteri"


def test_pick_window_strict_refuses_ambiguous(gw):
    with pytest.raises(ToolError, match="birden çok pencere"):
        desktop.pick_window(gw, "dosya gezgini", strict=True)
    # tam başlık verilince tek pencere
    assert desktop.pick_window(gw, "Not Defteri", strict=True).title == "Not Defteri"


@pytest.mark.parametrize("title", ["", "   "])
def test_pick_window_empty_title(gw, title):
    with pytest.raises(ToolError, match="boş"):
        desktop.pick_window(gw, title)


def test_pick_window_missing(gw):
    with pytest.raises(ToolError, match="pencere yok"):
        desktop.pick_window(gw, "Discord")


# --------------------------------------------------------------- pencere işlemleri


def test_list_windows_mac_drops_blank_titles(gw):
    titles = desktop.list_windows()
    assert "Not Defteri" in titles and "" not in titles and "   " not in titles


def test_list_windows_without_pygetwindow(monkeypatch):
    monkeypatch.setattr(desktop, "IS_MAC", True)
    monkeypatch.setitem(sys.modules, "pygetwindow", None)
    with pytest.raises(ToolError, match="pygetwindow"):
        desktop.list_windows()


def test_list_windows_linux_wmctrl(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WIN", False)
    monkeypatch.setattr(desktop, "IS_MAC", False)
    monkeypatch.setattr(desktop.shutil, "which", lambda n: "/usr/bin/wmctrl")
    out = "0x01  0 host Firefox - İndirilenler\n0x02  0 host\n"
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=out))
    assert desktop.list_windows() == ["Firefox - İndirilenler"]
    monkeypatch.setattr(desktop.shutil, "which", lambda n: None)
    with pytest.raises(ToolError, match="wmctrl"):
        desktop.list_windows()


def test_focus_window_mac_restores_minimized(gw):
    gw[4].isMinimized = True
    assert desktop.focus_window("not defteri") == "Not Defteri"
    assert gw[4].calls == ["restore", "activate"]


def test_window_op_close_ambiguous_does_not_close_anything(gw):
    with pytest.raises(ToolError, match="birden çok"):
        desktop.window_op("Dosya Gezgini", "close")
    assert not any(w.calls for w in gw)


def test_window_op_minimize_picks_best_match(gw):
    assert desktop.window_op("not defteri", "minimize") == "Not Defteri"
    assert gw[4].calls == ["minimize"]


def test_window_op_rejects_unknown_op_and_blank_title(gw):
    with pytest.raises(ToolError, match="bilinmeyen pencere işlemi"):
        desktop.window_op("Not Defteri", "kill")
    with pytest.raises(ToolError, match="boş"):
        desktop.window_op("  ", "close")


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WIN", False)
    monkeypatch.setattr(desktop, "IS_MAC", False)
    calls: list[list[str]] = []
    state = {"rc": 0, "exc": None}

    def run(cmd, **kw):
        calls.append(cmd)
        if state["exc"]:
            raise state["exc"]
        return SimpleNamespace(returncode=state["rc"], stdout=b"", stderr=b"")

    monkeypatch.setattr(desktop.subprocess, "run", run)
    return calls, state


def test_focus_window_linux_reports_missing_window(linux):
    calls, state = linux
    assert desktop.focus_window("Firefox") == "Firefox"
    assert calls[-1] == ["wmctrl", "-a", "Firefox"]
    state["rc"] = 1  # eskiden sessizce "öne alındı" diyordu
    with pytest.raises(ToolError, match="pencere yok"):
        desktop.focus_window("Yok")
    with pytest.raises(ToolError, match="boş"):
        desktop.focus_window(" ")


def test_wmctrl_missing_or_hung(linux):
    _, state = linux
    state["exc"] = FileNotFoundError("wmctrl")
    with pytest.raises(ToolError, match="wmctrl gerekli"):
        desktop.window_op("x", "close")
    state["exc"] = subprocess.TimeoutExpired("wmctrl", 10)
    with pytest.raises(ToolError, match="cevap vermedi"):
        desktop.focus_window("x")


def test_window_op_linux(linux):
    calls, _ = linux
    assert desktop.window_op("Firefox", "close") == "Firefox"
    assert desktop.window_op("Firefox", "maximize") == "Firefox"
    assert calls == [["wmctrl", "-c", "Firefox"],
                     ["wmctrl", "-r", "Firefox", "-b", "add,maximized_vert,maximized_horz"]]
    with pytest.raises(ToolError, match="desteklenmiyor"):
        desktop.window_op("Firefox", "minimize")


def test_force_foreground_noop_off_windows(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WIN", False)
    assert desktop.force_foreground(123) is True


# ------------------------------------------------------------------ open / risk


@pytest.mark.parametrize("target,script", [
    ("C:/x/run.bat", True), ("run.BAT ", True), ('"C:/x/kur.msi"', True), ("x.bat.", True), ("x.ps1. . ", True),
    ("x.bat::$DATA", True), ("file:///C:/x/run.bat", True), ("ayar.reg", True), ("app.jar", True),
    ("https://site.com/kur.msi", False), ("http://a.com/x.bat?q=1", False), ("notepad", False),
    ("C:/x/app.exe", False), ("rapor.pdf", False),
])
def test_is_script_target(target, script):
    assert desktop.is_script_target(target) is script


def test_windows_risk_levels():
    t = desktop.Windows()
    assert t.risk_for({"action": "list"}) == READ
    assert t.risk_for({"action": "find", "target": "x"}) == READ
    assert t.risk_for({"action": "open", "target": "kur.bat."}) == EXEC  # sondaki nokta kaçış yolu değil
    assert t.risk_for({"action": "open", "target": None}) == COMPUTER
    for a in ("close", "minimize", "focus", "navigate"):
        assert t.risk_for({"action": a, "title": "x"}) == COMPUTER
    assert desktop.UIElements().risk == READ
    assert desktop.UIAct().risk == COMPUTER
    c = desktop.Clipboard()
    assert c.risk_for({"action": "get"}) == READ and c.risk_for({"action": "set"}) == COMPUTER


@pytest.mark.parametrize("target", ["", "   ", None])
def test_open_app_rejects_empty_target(target, monkeypatch):
    monkeypatch.setattr(desktop, "IS_WIN", True)  # Windows'ta Path("") "." -> çalışma klasörü açılıyordu
    with pytest.raises(ToolError, match="boş"):
        desktop.open_app(target)


def test_open_app_linux_and_mac(monkeypatch):
    launched: list[list[str]] = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    monkeypatch.setattr(desktop, "IS_WIN", False)
    monkeypatch.setattr(desktop, "IS_MAC", False)
    monkeypatch.setattr(desktop.shutil, "which", lambda n: "/usr/bin/gedit" if n == "gedit" else None)
    assert desktop.open_app(" gedit ") == "açıldı: gedit"
    desktop.open_app("https://example.com")
    assert launched == [["/usr/bin/gedit"], ["xdg-open", "https://example.com"]]
    launched.clear()
    monkeypatch.setattr(desktop, "IS_MAC", True)
    desktop.open_app("Safari")
    desktop.open_app("https://example.com")
    assert launched == [["open", "-a", "Safari"], ["open", "https://example.com"]]


def test_open_app_windows_rejects_cmd_metacharacters(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WIN", True)
    monkeypatch.setattr(desktop.shutil, "which", lambda n: None)
    monkeypatch.setattr(desktop, "_start_menu_lookup", lambda n: None)
    monkeypatch.setattr(desktop, "match_start_app", lambda n: None)
    monkeypatch.setattr(desktop, "find_installed_apps", lambda n: [])
    popen: list = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda *a, **k: popen.append(a))
    with pytest.raises(ToolError, match="geçersiz karakter"):
        desktop.open_app("yokprogram&calc")
    assert popen == []


# --------------------------------------------------------------- Windows aracı


def test_windows_run_validates_arguments(ctx):
    t = desktop.Windows()
    for action in ("find", "open", "navigate", "switch_tab"):
        with pytest.raises(ToolError, match="target"):
            t.run(ctx, action, target="   ")
    for action in ("focus", "close", "minimize"):
        with pytest.raises(ToolError, match="title gerekli"):
            t.run(ctx, action, title="  ")  # boşluk başlık ilk pencereyi kapatıyordu


def test_windows_run_coerces_new_tab_string(ctx, monkeypatch):
    seen: list = []
    monkeypatch.setattr(desktop, "navigate_tab", lambda target, title, new_tab: seen.append(new_tab) or "ok")
    t = desktop.Windows()
    t.run(ctx, "navigate", target="github.com", new_tab="false")  # bool("false") True idi: yeni sekme açıyordu
    t.run(ctx, "navigate", target="github.com", new_tab="true")
    t.run(ctx, "navigate", target="github.com")
    assert seen == [False, True, None]
    with pytest.raises(ToolError, match="new_tab"):
        t.run(ctx, "navigate", target="github.com", new_tab="belki")


def test_windows_run_dispatch(ctx, monkeypatch, no_sleep):
    monkeypatch.setattr(desktop, "list_windows", lambda: [])
    assert desktop.Windows().run(ctx, "list").text == "pencere yok"
    monkeypatch.setattr(desktop, "find_installed_apps", lambda t: [])
    assert "bulunamadı" in desktop.Windows().run(ctx, "find", target="photoshop").text
    monkeypatch.setattr(desktop, "find_installed_apps", lambda t: ["PATH: /usr/bin/x"])
    assert desktop.Windows().run(ctx, "find", target="x").text == "PATH: /usr/bin/x"
    monkeypatch.setattr(desktop, "open_app", lambda t: f"açıldı: {t}")
    assert desktop.Windows().run(ctx, "open", target=" notepad ").text == "açıldı: notepad"
    monkeypatch.setattr(desktop, "switch_tab", lambda t, title: None)
    assert "açık sekme yok" in desktop.Windows().run(ctx, "switch_tab", target="Gmail").text
    monkeypatch.setattr(desktop, "focus_window", lambda t: t.upper())
    assert desktop.Windows().run(ctx, "focus", title="discord").text == "öne alındı: DISCORD"
    monkeypatch.setattr(desktop, "window_op", lambda t, op: t)
    assert desktop.Windows().run(ctx, "close", title=" Discord ").text == "close: Discord"


def test_switch_tab_matches_turkish_titles(monkeypatch, no_sleep):
    titles = iter(["Gmail - Opera", "İNDİRİLENLER - Opera"])
    keys: list[str] = []
    monkeypatch.setattr(desktop, "focus_window", lambda t: t)
    monkeypatch.setattr(desktop, "active_title", lambda: next(titles))
    monkeypatch.setitem(sys.modules, "alice.tools.computer",
                        SimpleNamespace(press_keys=keys.append, type_text=lambda t: None))
    assert desktop.switch_tab("indirilenler", "Opera") == "İNDİRİLENLER - Opera"
    assert keys == ["ctrl+tab"]
    with pytest.raises(ToolError, match="boş"):
        desktop.switch_tab("  ", "Opera")


def test_switch_tab_stops_after_full_cycle(monkeypatch, no_sleep):
    seq = iter(["A - Opera", "B - Opera", "A - Opera"])
    monkeypatch.setattr(desktop, "focus_window", lambda t: t)
    monkeypatch.setattr(desktop, "active_title", lambda: next(seq))
    pressed: list[str] = []
    monkeypatch.setitem(sys.modules, "alice.tools.computer",
                        SimpleNamespace(press_keys=pressed.append, type_text=lambda t: None))
    assert desktop.switch_tab("zzz", "Opera") is None
    assert len(pressed) == 2


def test_current_tab_url_restores_clipboard(monkeypatch, no_sleep):
    clip = SimpleNamespace(content="gizli", copies=[])

    def copy(t):
        clip.copies.append(t)
        clip.content = t

    keys: list[str] = []

    def press(k):
        keys.append(k)
        if k == "ctrl+c":
            clip.content = " https://a.com/x "

    monkeypatch.setitem(sys.modules, "pyperclip", SimpleNamespace(paste=lambda: clip.content, copy=copy))
    monkeypatch.setitem(sys.modules, "alice.tools.computer", SimpleNamespace(press_keys=press))
    monkeypatch.setattr(desktop, "focus_window", lambda t: t)
    assert desktop.current_tab_url("Opera") == "https://a.com/x"
    assert keys == ["ctrl+l", "ctrl+c", "esc"] and clip.content == "gizli"


def test_navigate_tab_opens_default_browser_when_not_running(monkeypatch):
    def nope(t):
        raise ToolError("yok")

    opened: list[str] = []
    monkeypatch.setattr(desktop, "focus_window", nope)
    monkeypatch.setattr(desktop, "open_app", opened.append)
    assert "açık değildi" in desktop.navigate_tab("kedi videoları", "Opera")
    assert opened == ["https://www.google.com/search?q=kedi videoları"]


def test_active_title_without_pygetwindow(monkeypatch):
    monkeypatch.setitem(sys.modules, "pygetwindow", None)
    assert desktop.active_title() == ""


def test_browser_window_title(monkeypatch):
    import alice.tools.browser as browser

    monkeypatch.setattr(browser, "default_browser_exe", lambda: r"C:\Program Files\Google\Chrome\chrome.exe")
    assert desktop.browser_window_title() == "Chrome"
    monkeypatch.setattr(browser, "default_browser_exe", lambda: None)
    assert desktop.browser_window_title() == "Opera"


# ----------------------------------------------------------- uygulama arama


def test_match_start_app_turkish_and_exact_first(monkeypatch):
    apps = [("Ayarlar Yardımcısı", "x!1"), ("Ayarlar", "windows.immersivecontrolpanel"), ("İnternet Radyo", "r!1")]
    monkeypatch.setattr(desktop, "start_apps", lambda refresh=False: apps)
    assert desktop.match_start_app("ayarlar") == ("Ayarlar", "windows.immersivecontrolpanel")
    assert desktop.match_start_app("internet") == ("İnternet Radyo", "r!1")
    assert desktop.match_start_app("!!") is None
    assert desktop.match_start_app("yok") is None


def test_start_apps_off_windows_is_empty(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WIN", False)
    monkeypatch.setattr(desktop, "_START_APPS", None)
    assert desktop.start_apps(refresh=True) == []


def test_start_menu_lookup_prefers_exact_then_shortest(tmp_path, monkeypatch):
    root = tmp_path / "Programs"
    (root / "Sub").mkdir(parents=True)
    for name in ("İnternet Yöneticisi Pro.lnk", "İnternet Yöneticisi.lnk", "Sub/readme.txt"):
        (root / name).write_bytes(b"")
    monkeypatch.setattr(desktop, "_shortcut_roots", lambda: [root, tmp_path / "yok"])
    assert desktop._start_menu_lookup("internet yöneticisi").name == "İnternet Yöneticisi.lnk"
    assert desktop._start_menu_lookup("İNTERNET").name == "İnternet Yöneticisi.lnk"
    assert desktop._start_menu_lookup("  ") is None
    assert desktop._start_menu_lookup("readme") is None


def test_exes_in_skips_installers(tmp_path):
    (tmp_path / "bin").mkdir()
    for n in ("game.exe", "bin/game-helper.exe", "unins000.exe", "notes.txt"):
        (tmp_path / n).write_bytes(b"")
    found = {p.name for p in desktop._exes_in(tmp_path, "game")}
    assert found == {"game.exe"}
    assert {p.name for p in desktop._exes_in(tmp_path, "zzz")} == {"game.exe"}


def test_steam_libraries_reads_vdf(tmp_path, monkeypatch):
    steam = tmp_path / "x86" / "Steam"
    (steam / "steamapps" / "common").mkdir(parents=True)
    lib = tmp_path / "D" / "SteamLibrary"
    (lib / "steamapps" / "common").mkdir(parents=True)
    vdf = '"libraryfolders" { "1" { "path" "%s" } }' % str(lib).replace("\\", "\\\\")
    (steam / "steamapps" / "libraryfolders.vdf").write_text(vdf, encoding="utf-8")
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "x86"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "none"))
    libs = desktop._steam_libraries()
    assert steam / "steamapps" / "common" in libs and lib / "steamapps" / "common" in libs


def test_find_installed_apps_off_windows(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WIN", False)
    monkeypatch.setattr(desktop.shutil, "which", lambda n: "/usr/bin/" + n if n == "vim" else None)
    assert desktop.find_installed_apps("vim") == ["PATH: /usr/bin/vim"]
    assert desktop.find_installed_apps("yok") == []


def test_resolve_lnks_ignores_non_lnk_and_errors(monkeypatch, tmp_path):
    assert desktop._resolve_lnks([tmp_path / "a.url"]) == {}

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("powershell", 15)

    monkeypatch.setattr(desktop.subprocess, "run", boom)
    assert desktop._resolve_lnks([tmp_path / "a.lnk"]) == {}
    out = f"{tmp_path / 'a.lnk'}|C:\\app.exe\nbozuk satır\n".encode()
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=out))
    assert desktop._resolve_lnks([tmp_path / "a.lnk"]) == {tmp_path / "a.lnk": "C:\\app.exe"}


# ------------------------------------------------------------------ UIA


class Rect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class FakeEl:
    def __init__(self, ctype, name="", rect=(0, 0, 50, 20), visible=True, value=None, auto_id="", boom=False):
        self.element_info = SimpleNamespace(control_type=ctype, name=name, rectangle=Rect(*rect),
                                            automation_id=auto_id)
        self._visible, self._value, self.boom = visible, value, boom
        self.calls: list[tuple] = []

    def is_visible(self):
        if self.boom:
            raise RuntimeError("öğe kayboldu")
        return self._visible

    def is_enabled(self):
        return True

    def get_value(self):
        return self._value

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a: self.calls.append((name, *a))


class FakeUIAWin:
    def __init__(self, els, title="Hesap Makinesi"):
        self.els, self.title = els, title

    def iter_descendants(self, depth=14):
        return iter(self.els)

    def window_text(self):
        return self.title


def test_collect_ui_elements_filters_and_numbers(ctx, monkeypatch):
    els = [FakeEl("Button", "Bir"), FakeEl("Text", "etiket"), FakeEl("Button", "gizli", visible=False),
           FakeEl("Button", "küçük", rect=(0, 0, 1, 1)), FakeEl("Edit", "", value="42"), FakeEl("Button", ""),
           FakeEl("Button", "x", boom=True), FakeEl("MenuItem", "Dosya", auto_id="m1")]
    monkeypatch.setattr(desktop, "IS_WIN", True)
    monkeypatch.setattr(desktop, "_foreground_uia", lambda title: FakeUIAWin(els))
    title, items = desktop.collect_ui_elements(ctx, limit=10)
    assert title == "Hesap Makinesi"
    assert [(i["id"], i["role"], i["name"], i["value"]) for i in items] == [
        (1, "Button", "Bir", ""), (2, "Edit", "", "42"), (3, "MenuItem", "Dosya", "")]
    assert ctx.state["ui_elems"][2] is els[4] and items[0]["center"] == (25, 10)
    _, items = desktop.collect_ui_elements(ctx, limit=1)
    assert len(items) == 1
    text = desktop.format_ui(title, items)
    assert text.splitlines() == ["pencere: Hesap Makinesi", "[1] Button 'Bir'"]


def test_collect_ui_elements_does_not_read_document_text(ctx, monkeypatch):
    """WebView2 Document'ının değeri sayfanın bütün metni: okumak bir dakika sürüyordu."""
    class Doc(FakeEl):
        def get_value(self):
            raise AssertionError("Document metni okunmamalı")

    monkeypatch.setattr(desktop, "IS_WIN", True)
    monkeypatch.setattr(desktop, "_foreground_uia", lambda title: FakeUIAWin([Doc("Document", "")], "ALICE"))
    _, items = desktop.collect_ui_elements(ctx)
    assert [(i["role"], i["value"]) for i in items] == [("Document", "")]


def test_collect_ui_elements_off_windows(ctx, monkeypatch):
    monkeypatch.setattr(desktop, "IS_WIN", False)
    with pytest.raises(ToolError, match="sadece Windows"):
        desktop.collect_ui_elements(ctx)


def test_foreground_uia_turkish_title(monkeypatch):
    wins = [FakeUIAWin([], "Belgeler"), FakeUIAWin([], "İndirilenler")]
    mod = types.ModuleType("pywinauto")
    mod.Desktop = lambda backend: SimpleNamespace(windows=lambda: wins)
    monkeypatch.setitem(sys.modules, "pywinauto", mod)
    assert desktop._foreground_uia("indirilenler") is wins[1]
    with pytest.raises(ToolError, match="pencere yok"):
        desktop._foreground_uia("Discord")
    monkeypatch.setitem(sys.modules, "pywinauto", None)
    with pytest.raises(ToolError, match="pywinauto"):
        desktop._foreground_uia("x")


def test_ui_elements_limit_coercion(ctx, monkeypatch):
    seen: list = []
    monkeypatch.setattr(desktop, "collect_ui_elements", lambda c, t, limit: seen.append(limit) or ("W", []))
    tool = desktop.UIElements()
    tool.run(ctx, limit="20")
    tool.run(ctx, limit=0)
    tool.run(ctx, limit=99999)
    tool.run(ctx, limit=None)
    assert seen == [20, 1, 500, 150]
    with pytest.raises(ToolError, match="limit sayı olmalı"):
        tool.run(ctx, limit="çok")


def test_ui_act_coerces_id_and_validates(ctx, monkeypatch, no_sleep):
    el = FakeEl("Button", "Tamam")
    ctx.state["ui_elems"] = {3: el}
    ctx.state["ui_items"] = {3: {"role": "Button", "name": "Tamam"}}
    act = desktop.UIAct()
    assert act.run(ctx, element_id="3", action="click").text == "click -> [3] Button 'Tamam'"
    assert act.run(ctx, element_id=3.0, action="toggle").text.startswith("toggle")
    assert el.calls == [("invoke",), ("toggle",)]
    with pytest.raises(ToolError, match="element_id sayı olmalı"):
        act.run(ctx, element_id="üç", action="click")
    with pytest.raises(ToolError, match="öğe bulunamadı"):
        act.run(ctx, element_id=9, action="click")
    with pytest.raises(ToolError, match="text gerekli"):
        act.run(ctx, element_id=3, action="set_text")
    with pytest.raises(ToolError, match="bilinmeyen aksiyon"):
        act.run(ctx, element_id=3, action="sil")
    act.run(ctx, element_id=3, action="set_text", text=123)
    assert el.calls[-1] == ("set_edit_text", "123")


def test_ui_act_click_falls_back_to_click_input(ctx, no_sleep):
    class NoInvoke(FakeEl):
        def invoke(self):
            raise RuntimeError("invoke pattern yok")

    el = NoInvoke("ListItem", "öğe")
    ctx.state["ui_elems"] = {1: el}
    desktop.ui_act(ctx, 1, "click")
    desktop.ui_act(ctx, 1, "double_click")
    desktop.ui_act(ctx, 1, "right_click")
    desktop.ui_act(ctx, 1, "focus")
    assert [c[0] for c in el.calls] == ["click_input", "double_click_input", "right_click_input", "set_focus"]


# ------------------------------------------------------------------ pano


def test_clipboard_set_requires_text(ctx, monkeypatch):
    copies: list[str] = []
    monkeypatch.setitem(sys.modules, "pyperclip", SimpleNamespace(paste=lambda: "", copy=copies.append))
    c = desktop.Clipboard()
    with pytest.raises(ToolError, match="text gerekli"):
        c.run(ctx, action="set")  # eskiden panoyu sessizce boşaltıyordu
    assert copies == []
    assert c.run(ctx, action="set", text=42).text == "panoya kopyalandı"
    assert copies == ["42"]
    assert c.run(ctx, action="set", text="").text == "panoya kopyalandı"
    assert c.run(ctx, action="get").text == "(pano boş)"
    with pytest.raises(ToolError, match="bilinmeyen action"):
        c.run(ctx, action="sil")


def test_clipboard_without_pyperclip(ctx, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyperclip", None)
    with pytest.raises(ToolError, match="pyperclip"):
        desktop.Clipboard().run(ctx, action="get")
