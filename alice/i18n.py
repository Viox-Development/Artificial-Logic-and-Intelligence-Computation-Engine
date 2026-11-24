"""Text shown to the user, in the user's language (#214).

Source strings are English and are written in the code wrapped in `_()`:

    console.print(_("Session loaded: {sid} ({n} messages)", sid=sid, n=len(msgs)))

Translations live in `alice/locales/<lang>.json` as {"English text": "translated text"}; a string without a
translation is shown in English. Placeholders use `str.format` names, and a translation must keep the same names
(tests/test_i18n.py checks this, and that every `_()` string in the code has a Turkish translation).

The language is `[ui] language` in config.toml ("en" or "tr", default "en"; the setup wizard and Settings write it).
The ALICE_LANG environment variable overrides it, for scripts and tests.

Speech follows it too: `[voice] language` is the language that is recognised and spoken; "auto" (the default) means
the interface language, a code like "tr" or "en" pins it (speech_language()).
"""

from __future__ import annotations

import json
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

SUPPORTED = ("en", "tr")
DEFAULT = "en"
LOCALES = Path(__file__).with_name("locales")

_lock = threading.Lock()
_current: str | None = None


def _normalize(value: Any) -> str:
    v = str(value or "").strip().lower().replace("_", "-").split("-")[0]
    return v if v in SUPPORTED else DEFAULT


def _from_config() -> str:
    """[ui] language from config.toml, read directly so that importing this module stays cheap."""
    try:
        from alice.config import alice_home, tomllib

        path = alice_home() / "config.toml"
        data = tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    except Exception:  # noqa: BLE001 - a broken config must not break every message; load_config reports it
        data = {}
    return _normalize((data.get("ui") or {}).get("language"))


def language() -> str:
    """The current language code ("en" or "tr")."""
    global _current
    env = os.environ.get("ALICE_LANG")
    if env:
        return _normalize(env)
    with _lock:
        if _current is None:
            _current = _from_config()
        return _current


def set_language(value: str | None) -> str:
    """Switch the language for this process (Settings calls it after writing the config). None: read it again."""
    global _current
    with _lock:
        _current = _normalize(value) if value is not None else None
    return language()


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    """{English: translation} for a language; {} for English or a missing file."""
    if lang == DEFAULT:
        return {}
    try:
        data = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _(text: str, /, **values: Any) -> str:
    """`text` in the user's language, with `values` filled in."""
    out = catalog(language()).get(text, text)
    if not values:
        return out
    try:
        return out.format(**values)
    except (KeyError, IndexError, ValueError):
        return text.format(**values)  # a broken translation falls back to English rather than failing


# Speech: BCP-47 tags for the speech services and the free Edge voice used as the last fallback, per language.
SPEECH_TAGS = {"en": "en-US", "tr": "tr-TR"}
EDGE_VOICES = {"en": "en-US-AriaNeural", "tr": "tr-TR-EmelNeural"}


def speech_language(value: Any = "auto") -> str:
    """The language for speech recognition and synthesis: `[voice] language` when it names one, otherwise ("auto" or
    empty) the interface language."""
    v = str(value or "").strip().lower()
    return v if v and v != "auto" else language()


def speech_tag(value: Any = "auto") -> str:
    """BCP-47 tag of the speech language ("tr-TR"), for services that want one."""
    lang = speech_language(value)
    return SPEECH_TAGS.get(lang, lang)


def edge_voice(setting: Any, value: Any = "auto") -> str:
    """[voice] tts_edge: "auto" picks the Edge voice of the speech language, empty turns it off, a name is used as is."""
    v = str(setting or "").strip()
    if v.lower() != "auto":
        return v
    return EDGE_VOICES.get(speech_language(value), EDGE_VOICES["en"])


def pick(en: str, tr: str) -> str:
    """For the rare text that is built in code instead of looked up: the English or the Turkish one."""
    return tr if language() == "tr" else en
