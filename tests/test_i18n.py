"""User-facing text in the user's language (#214): alice/i18n.py and alice/locales/*.json."""

import ast
import json
import re
import string
from pathlib import Path

import pytest

from alice import i18n

ROOT = Path(i18n.__file__).parent


@pytest.fixture(autouse=True)
def fresh_language(monkeypatch, tmp_path):
    monkeypatch.delenv("ALICE_LANG", raising=False)
    monkeypatch.setenv("ALICE_HOME", str(tmp_path))
    i18n.set_language(None)
    i18n.catalog.cache_clear()
    yield
    i18n.set_language(None)  # the catalog may be monkeypatched here; it is cleared again at the next setup


def test_language_comes_from_config_and_env(monkeypatch, tmp_path):
    assert i18n.language() == "en"  # no config: English
    (tmp_path / "config.toml").write_text('[ui]\nlanguage = "tr"\n', encoding="utf-8")
    i18n.set_language(None)
    assert i18n.language() == "tr"
    monkeypatch.setenv("ALICE_LANG", "en_US")
    assert i18n.language() == "en"
    monkeypatch.setenv("ALICE_LANG", "de")  # unsupported: the default
    assert i18n.language() == "en"


def test_broken_config_falls_back_to_english(tmp_path):
    (tmp_path / "config.toml").write_text("[ui\nlanguage = ", encoding="utf-8")
    i18n.set_language(None)
    assert i18n.language() == "en"


def test_translate_and_fill_in(monkeypatch):
    monkeypatch.setattr(i18n, "catalog", lambda lang: {"Hello {name}": "Merhaba {name}"} if lang == "tr" else {})
    assert i18n._("Hello {name}", name="Ayşe") == "Hello Ayşe"
    i18n.set_language("tr")
    assert i18n._("Hello {name}", name="Ayşe") == "Merhaba Ayşe"
    assert i18n._("Not translated") == "Not translated"
    assert i18n.pick("yes", "evet") == "evet"


def test_broken_translation_falls_back_to_english(monkeypatch):
    monkeypatch.setattr(i18n, "catalog", lambda lang: {"{n} files": "{count} dosya"})
    i18n.set_language("tr")
    assert i18n._("{n} files", n=3) == "3 files"


def _fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


STATIC = ROOT / "app" / "static"
# _("...") or _('...') in the app's scripts; template literals are not translatable (use {name} placeholders)
JS_CALL = re.compile(r"""(?<![\w.$])_\(\s*(?:"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)')""")
HTML_TEXT = re.compile(r"<\w+[^>]*\bdata-i18n(?![-\w])[^>]*>([^<]*)<")
HTML_ATTR = re.compile(r"<\w+([^>]*\bdata-i18n-attr=\"([^\"]+)\"[^>]*)>")


def _js_literal(double: str | None, single: str | None) -> str:
    body = double if double is not None else (single or "").replace("\\'", "'").replace('"', '\\"')
    return json.loads(f'"{body}"')


def _app_strings() -> dict[str, str]:
    """Strings the app translates: _("...") in static/*.js, data-i18n text and data-i18n-attr attributes in *.html."""
    found = {}
    for p in sorted(STATIC.glob("*.js")):
        if p.name.endswith(".min.js") or p.name == "highlight.js":
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("//"):
                continue
            for m in JS_CALL.finditer(line):
                found.setdefault(_js_literal(m.group(1), m.group(2)), f"static/{p.name}:{n}")
    for p in sorted(STATIC.glob("*.html")):
        html = p.read_text(encoding="utf-8")
        for m in HTML_TEXT.finditer(html):
            found.setdefault(m.group(1).strip(), f"static/{p.name}")
        for m in HTML_ATTR.finditer(html):
            for name in m.group(2).split():
                v = re.search(rf'\b{re.escape(name)}="([^"]*)"', m.group(1))
                assert v, f"static/{p.name}: data-i18n-attr names {name!r} but the element has no such attribute"
                found.setdefault(v.group(1), f"static/{p.name}")
    return found


def _source_strings() -> dict[str, str]:
    """Every literal passed to _() in alice/ (Python and the app), with where it is."""
    found = _app_strings()
    for p in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "spoken")
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.setdefault(node.args[0].value, f"{p.relative_to(ROOT.parent)}:{node.lineno}")
    return found


def test_every_message_has_a_turkish_translation_with_the_same_placeholders():
    tr = json.loads((ROOT / "locales" / "tr.json").read_text(encoding="utf-8"))
    missing = [f"{where}: {text!r}" for text, where in _source_strings().items() if text not in tr]
    assert not missing, "add these to alice/locales/tr.json:\n" + "\n".join(missing)
    wrong = [k for k, v in tr.items() if _fields(k) != _fields(v)]
    assert not wrong, f"placeholders differ: {wrong}"


def test_locale_files_are_flat_string_maps():
    for f in (ROOT / "locales").glob("*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        assert isinstance(data, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()), f


def test_cli_messages_follow_the_language():
    assert i18n._("Permission needed") == "Permission needed"
    i18n.set_language("tr")
    assert i18n._("Permission needed") == "İzin gerekiyor"
    assert i18n._("[green]Session loaded:[/] {sid} ({n} messages)", sid="x", n=2) == "[green]oturum yüklendi:[/] x (2 mesaj)"


def _binds_underscore(node: ast.AST) -> bool:
    """True when the node rebinds the name `_` (assignment, loop/with/except target, comprehension, parameter)."""
    if isinstance(node, ast.Name) and node.id == "_" and isinstance(node.ctx, ast.Store):
        return True
    if isinstance(node, ast.arg) and node.arg == "_":
        return True
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_"


def test_modules_that_translate_never_rebind_underscore():
    """`agent, speaker, _ = ...` in a module that imports _ from alice.i18n turns every later _() in that function
    into a call on the unpacked value (it crashed `alice core run` with no microphone)."""
    bad = []
    for p in ROOT.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        imports = any(isinstance(n, ast.ImportFrom) and n.module == "alice.i18n" and any(a.name == "_" for a in n.names)
                      for n in ast.walk(tree))
        if imports and p.name != "i18n.py":
            bad += [f"{p.relative_to(ROOT.parent)}:{n.lineno}" for n in ast.walk(tree) if _binds_underscore(n)]
    assert not bad, "rename these to something other than _:\n" + "\n".join(bad)


def test_app_strings_are_found():
    """Guards the scanner itself: the quick bar's strings must be seen, or a broken regex would pass everything."""
    found = _app_strings()
    assert "thinking…" in found and "Always in this project" in found  # quick.js
    assert "Projects" in found and "Ask ALICE… (Enter sends, Esc closes)" in found and "Speak" in found  # quick.html


def test_pages_carry_the_language_and_its_catalog(monkeypatch):
    from alice.app import server

    monkeypatch.setattr(i18n, "catalog", lambda lang: {"Projects": "Projeler</script>"} if lang == "tr" else {})
    page = server._page("quick.html", "tok")
    assert '<html lang="en">' in page and '"catalog": {}' in page and "{{" not in page
    i18n.set_language("tr")
    page = server._page("quick.html", "tok")
    block = re.search(r'<script type="application/json" id="alice-i18n">(.*?)</script>', page).group(1)
    assert '<html lang="tr">' in page and json.loads(block) == {"lang": "tr", "catalog": {"Projects": "Projeler</script>"}}
    assert page.index('id="alice-i18n"') < page.index("/static/i18n.js") < page.index("/static/quick.js")


def test_model_reasons_stay_english_and_are_shown_in_the_language():
    """Router.available reasons go into errors and logs (English), the cooldown parser reads both the new and the old
    Turkish wording, and only the display text follows the interface language."""
    from alice.providers.router import COOLING_RX, is_ready, reason_text
    from alice.team import human_failure

    assert COOLING_RX.search("g:m: cooling down (12 s)").group(1) == "12"
    assert COOLING_RX.search("g:m: soğumada (30 sn)").group(1) == "30"
    assert is_ready("ready") and is_ready("hazır") and not is_ready("model not found")
    assert reason_text("cooling down (12 s)") == "cooling down (12 s)" and reason_text("no GROQ_API_KEY") == "no GROQ_API_KEY"
    i18n.set_language("tr")
    assert reason_text("cooling down (12 s)") == "soğumada (12 sn)" and reason_text("no GROQ_API_KEY") == "GROQ_API_KEY yok"
    assert reason_text("ready") == "hazır" and reason_text("something new") == "something new"
    assert "2" in human_failure(["g:m: cooling down (7200 s)"])


def test_old_turkish_default_rules_follow_the_language(tmp_path):
    """rules.json written before the switch holds the Turkish default text; it is read as the English key, so the
    rule is shown and notified in the interface language. A rule the user wrote is left as it is."""
    from alice.app.automations import rule_view
    from alice.core.rules import RuleEngine, RuleSet

    path = tmp_path / "rules.json"
    path.write_text(json.dumps([
        {"metric": "pc.gpu.temp", "op": ">", "threshold": 85, "message": "Ekran kartı {value:.0f}°C, birkaç dakikadır çok sıcak.",
         "duration": 180, "description": "GPU uzun süre çok ısınırsa söyle", "id": "gpu-hot"},
        {"metric": "pc.fan", "op": ">", "threshold": 1, "message": "Fan döndü", "description": "kendi kuralım", "id": "mine"}],
        ensure_ascii=False), encoding="utf-8")
    rules = {r.id: r for r in RuleSet(path).rules}
    gpu, mine = rules["gpu-hot"], rules["mine"]
    assert gpu.message.startswith("The graphics card") and mine.message == "Fan döndü"
    assert rule_view(gpu)["text"] == "tell me if the GPU stays very hot for a long time"
    i18n.set_language("tr")
    assert rule_view(gpu)["text"] == "GPU uzun süre çok ısınırsa söyle" and rule_view(mine)["text"] == "kendi kuralım"
    assert RuleEngine._format(gpu, "pc.gpu.temp", 91) == "Ekran kartı 91°C, birkaç dakikadır çok sıcak."


def test_slash_command_descriptions_follow_the_language(tmp_path):
    """The built-in command descriptions are English in the code and translated when the catalog is built; a
    command the user wrote keeps its own text."""
    from alice.commands import catalog

    (tmp_path / ".alice" / "commands").mkdir(parents=True)
    (tmp_path / ".alice" / "commands" / "mine.md").write_text("---\ndescription: benim komutum\n---\nyap", encoding="utf-8")
    en = {c["name"]: c for c in catalog(tmp_path)}
    assert en["help"]["description"] == "Show the commands" and en["mine"]["description"] == "benim komutum"
    i18n.set_language("tr")
    tr = {c["name"]: c for c in catalog(tmp_path)}
    assert tr["help"]["description"] == "Komutları göster" and tr["mine"]["description"] == "benim komutum"
    assert all(c["description"] for c in tr.values())


def test_repl_help_is_translated_line_for_line():
    """cli.HELP is one big message (not a literal in _()), so the scanner can't check it: make sure the Turkish text
    exists and has the same lines, and that the update/argparse help strings are in the catalog too."""
    from alice import cli

    tr = json.loads((ROOT / "locales" / "tr.json").read_text(encoding="utf-8"))
    assert cli.HELP in tr, "add cli.HELP to alice/locales/tr.json"
    assert cli.HELP.count("\n") == tr[cli.HELP].count("\n")
    assert [ln.split()[0] for ln in cli.HELP.splitlines()[1:19]] == [ln.split()[0] for ln in tr[cli.HELP].splitlines()[1:19]]


def test_old_turkish_conversation_markers_are_still_recognized():
    """Chats saved before the markers became English hold the Turkish wording; both are recognized."""
    from alice import session as sess
    from alice.agent import _is_request, _last_request

    assert sess.is_summary("[Summary of the earlier conversation]\nx") and sess.is_summary("[Önceki konuşmanın özeti]\nx")
    assert not sess.is_summary("a normal message") and not sess.is_summary(None)
    assert sess.summary_body("[Önceki konuşmanın özeti] abc") == "abc" and sess.summary_body("[Summary of the earlier conversation] abc") == "abc"
    assert sess.is_removed_screenshot("[eski ekran görüntüsü yer kazanmak için kaldırıldı]") and sess.is_removed_screenshot(sess.SHOT_REMOVED)
    old = {"role": "user", "content": "[Önceki konuşmanın özeti]\nx\n\n## Süren iş: kullanıcının son isteği (birebir)\nİndirmeyi bitir"}
    new = {"role": "user", "content": "[Summary of the earlier conversation]\nx\n\n## Ongoing work: the user's latest request (verbatim)\nFinish the download"}
    assert _last_request([old]) == "İndirmeyi bitir" and _last_request([new]) == "Finish the download"
    assert not _is_request(old) and not _is_request(new)
    first = sess.first_prompt([{"role": "user", "content": "[devam kontrolü] x", "_nudge": True},
                               {"role": "user", "content": sess.NUDGE_PREFIX + "x", "_nudge": True},
                               {"role": "user", "content": "gerçek istek"}])
    assert first == "gerçek istek"


def test_agent_notices_are_translated_and_old_wordings_are_still_read():
    """DENIAL_HALT and BUDGET_HALT go through _(variable), which the scanner can't see: check them here. The markers and
    error texts that other code matches are read in both their English and their old Turkish form."""
    from alice import agent as ag
    from alice import session as sess

    tr = json.loads((ROOT / "locales" / "tr.json").read_text(encoding="utf-8"))
    for text in (ag.DENIAL_HALT, ag.BUDGET_HALT):
        assert text in tr and _fields(text) == _fields(tr[text]), text[:40]
    i18n.set_language("tr")
    assert "durdum" in i18n._(ag.BUDGET_HALT, used=30, budget=20) and "30" in i18n._(ag.BUDGET_HALT, used=30, budget=20)
    new, old = ag._TOO_BIG.search("g:m: request ~9000 tokens, model limit 4000"), ag._TOO_BIG.search("g:m: istek ~9000 token, model sınırı 4000")
    assert new.group(3) == old.group(3) == "4000" and (new.group(1) or new.group(2)) == (old.group(1) or old.group(2)) == "9000"
    assert sess.is_stopped(sess.STOPPED) and sess.is_stopped(sess.OLD_STOPPED) and not sess.is_stopped("done")
    assert sess.strip_plan_note(sess.PLAN_NOTE + "x") == "x" and sess.strip_plan_note(sess.OLD_PLAN_NOTE + "y") == "y"
    assert ag._CATALOG_RX.search("a\n\n# Loadable tools\nx") and ag._CATALOG_RX.search("a\n\n# Yüklenebilir araçlar\nx")


def test_core_mode_names_are_translated():
    """The tray menu and the mode event show the core's mode names through _(MODE_NAMES[...]), which the scanner can't
    see: every name needs a Turkish entry."""
    from alice.core import service

    tr = json.loads((ROOT / "locales" / "tr.json").read_text(encoding="utf-8"))
    for name in service.MODE_NAMES.values():
        assert name in tr, name


def test_buddy_labels_and_google_setup_steps_are_translated():
    """Buddy labels and the Google setup steps go through _(variable), which the scanner can't see."""
    from alice import buddy, setup_google

    tr = json.loads((ROOT / "locales" / "tr.json").read_text(encoding="utf-8"))
    for table in (buddy.SPECIES_EN, buddy.RARITY_EN, buddy.MOOD_EN):
        assert set(table) >= ({*buddy.SPECIES} if table is buddy.SPECIES_EN else set())
        for text in table.values():
            assert text in tr, text
    assert set(buddy.RARITY_EN) == {r for r, *_rest in buddy.RARITIES}
    for title, _url, how in setup_google.STEPS:
        assert title in tr and how in tr, title


def test_catalogue_keys_are_sorted():
    # Sorted keys let parallel changes add translations in different places instead of all at the end,
    # so they don't conflict with each other.
    keys = list(json.loads((ROOT / "locales" / "tr.json").read_text(encoding="utf-8")))
    assert keys == sorted(keys), "sort alice/locales/tr.json: json.dumps(..., indent=1, sort_keys=True)"


def test_key_catalogue_descriptions_have_turkish_translations():
    # env_catalog() passes the English descriptions of ENV_TEMPLATE through _(), which the literal scan can't see.
    from alice import config

    tr = json.loads((ROOT / "locales" / "tr.json").read_text(encoding="utf-8"))
    raw = config._ if hasattr(config, "_") else None
    assert raw is not None
    config._ = lambda text, **kw: text  # read the English catalogue
    try:
        cat = config.env_catalog()
    finally:
        config._ = raw
    missing = sorted({e["help"] for e in cat if e["help"] and e["help"] not in tr} | {e["field"] for e in cat if e["field"] not in tr})
    assert not missing, "add these to alice/locales/tr.json:\n" + "\n".join(missing)


def test_watchdog_texts_have_turkish_translations():
    # GAME_LIMITS texts go through _() as variables, which the literal scan can't see.
    from alice.core.watchdogs import GAME_LIMITS

    tr = json.loads((ROOT / "locales" / "tr.json").read_text(encoding="utf-8"))
    missing = [text for _key, _limit, text in GAME_LIMITS if text not in tr]
    assert not missing, "add these to alice/locales/tr.json:\n" + "\n".join(missing)


def test_speech_language_follows_the_interface_unless_pinned(monkeypatch):
    from alice import i18n

    monkeypatch.delenv("ALICE_SPEECH_LANG", raising=False)

    monkeypatch.setenv("ALICE_LANG", "tr")
    assert i18n.speech_language("auto") == "tr" and i18n.speech_language("") == "tr" and i18n.speech_language("en") == "en"
    assert i18n.speech_tag("auto") == "tr-TR" and i18n.edge_voice("auto", "auto") == "tr-TR-EmelNeural"
    monkeypatch.setenv("ALICE_LANG", "en")
    assert i18n.edge_voice("auto", "auto") == "en-US-AriaNeural" and i18n.edge_voice("auto", "tr") == "tr-TR-EmelNeural"
    assert i18n.edge_voice("tr-TR-AhmetNeural") == "tr-TR-AhmetNeural" and i18n.edge_voice("") == ""
