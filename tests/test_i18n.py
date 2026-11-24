"""User-facing text in the user's language (#214): alice/i18n.py and alice/locales/*.json."""

import ast
import json
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


def _source_strings() -> dict[str, str]:
    """Every literal passed to _() in alice/, with where it is."""
    found = {}
    for p in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
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
