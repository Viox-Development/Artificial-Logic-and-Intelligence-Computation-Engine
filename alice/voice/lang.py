"""Language and junk filter in the voice pipeline: prevents non-Turkish or meaningless speech from becoming action or being spoken.

Gemini hears game/video/Discord audio as Spanish, German, Portuguese sentences ("Abre la canción Capiaça");
these sentences caused songs to play or Install to be clicked. The model also sometimes responds in a foreign
language or says "---".
The decision is lightweight and rule-based: writing system, characters not in Turkish, frequent foreign words.
"""

from __future__ import annotations

import re

# Non-Latin scripts (Greek, Cyrillic, Hebrew/Arabic, Indic, Thai, Japanese, Chinese, Korean)
_NON_LATIN = re.compile("[Ͱ-ӿ֐-ۿऀ-࿿぀-ヿ㐀-鿿가-힯]")
# Latin letters not in Turkish (â, î, û also exist in Turkish: not counted)
_FOREIGN_LETTERS = re.compile("[ñáéíóúàèìòùãõäëßêôœæøå¿¡]", re.I)
_TURKISH_LETTERS = re.compile("[çğışöüÇĞİŞÖÜ]")
_FOREIGN_WORDS = {
    # Spanish / Portuguese
    # Words that are also Turkish words ("el", "para", "der", "bin", "is") and those frequently spoken while speaking Turkish ("okay",
    # "yes", "sorry") are not in the list.
    "que", "los", "las", "una", "por", "con", "como", "está", "esta", "pero", "muy", "sí",
    "você", "não", "nao", "uma", "isso", "obrigado", "gracias", "hola", "bueno", "vamos", "aqui", "también",
    # German
    "die", "das", "und", "ich", "nicht", "ist", "ein", "eine", "mit", "auf", "bereit", "klar",
    "alles", "danke", "ja", "nein", "doch", "aber", "wir", "sie", "es", "gut",
    # English
    "the", "and", "you", "what", "this", "that", "with", "have", "are", "it's", "i'm",
    "please", "speech", "detected",
}
# Frequent words that are definitely Turkish: balances foreign word count ("ya", "bu", "bir")
_TURKISH_WORDS = {"bir", "bu", "şu", "ve", "ne", "mi", "mı", "mu", "mü", "ben", "sen", "var", "yok", "değil",
                  "evet", "hayır", "tamam", "aç", "kapat", "şunu", "bunu", "abi", "knk", "lan", "naber", "yap",
                  "olur", "çok", "daha", "sonra", "şimdi", "için", "gibi", "kadar", "oyun", "nasıl", "neden"}
_JUNK = re.compile(r"^[\W_]*$|no speech|speech detected|<\s*noise\s*>|^\(.*\)$", re.I)


def foreign(text: str) -> bool:
    """Is this utterance most likely not Turkish? False for short or uncertain text (to avoid accidentally muting)."""
    t = (text or "").strip()
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 6:
        return False
    if sum(1 for c in letters if _NON_LATIN.match(c)) / len(letters) > 0.3:
        return True
    if _TURKISH_LETTERS.search(t) and not _FOREIGN_LETTERS.search(t):
        return False
    words = [w.lower() for w in re.findall(r"[\w']+", t)]
    if not words:
        return False
    alien = sum(w in _FOREIGN_WORDS for w in words) + 2 * len(_FOREIGN_LETTERS.findall(t))
    native = sum(w in _TURKISH_WORDS for w in words) + len(_TURKISH_LETTERS.findall(t))
    return alien >= 2 and alien > native


def junk(text: str) -> bool:
    """Is it output without content ("---", "...", "<no speech detected>")?"""
    return bool(_JUNK.search((text or "").strip()))


# Words where the user corrects the previous turn: the most reliable sign of failure. That turn enters the test set.
_CORRECTION = re.compile(
    r"\b(demedim|söylemedim|seslenmedim|yanlış anla\w*|yanlış duy\w*|anlamad[ıi]n|saçmal\w*|ne alaka|"
    r"öyle de(me|mi)\w*|onu demi\w*|bunu demi\w*|sana de(me|mi)\w*|yapmad[ıi]n ki|olmad[ıi] ki)\b|^\s*hayır\b",
    re.I)


def correction(text: str) -> bool:
    """"Is this a correction like "I didn't tell you", "you misunderstood", "you're talking nonsense", "no, ..."? """
    return bool(_CORRECTION.search(text or ""))


# Narrow classes for corpus labeling (see corpus.correction_label)
_NOT_FOR_ME = re.compile(r"\b(sana (de(me|mi)\w*|söyle(me|mi)\w*|konuşmu\w*|değil)|seslenmedim|seni çağırmadım)\b", re.I)
_MISHEARD = re.compile(r"\b(yanlış anla\w*|yanlış duy\w*|anlamad[ıi]n|onu demedim|bunu demedim|öyle demedim)\b", re.I)


def not_for_me(text: str) -> bool:
    """"I didn't say to you", "I didn't call": wasn't addressed to ALICE (false wake-up)."""
    return bool(_NOT_FOR_ME.search(text or ""))


def misheard(text: str) -> bool:
    """"You misunderstood", "I didn't say that": was addressed to ALICE but misheard."""
    return bool(_MISHEARD.search(text or "")) and not not_for_me(text)
