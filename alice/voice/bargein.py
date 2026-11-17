"""How much of ALICE's reply was heard when the user interrupts (barge-in).

When the user cuts in while ALICE is speaking, the audio stops, but the model thought it had said the whole reply:
on "devam et" (continue) it either started over or treated the unheard part as already said and skipped it. Here
the heard and unheard parts of the reply are separated and given to the model as a short note.
"""

from __future__ import annotations

MIN_UNHEARD = 25  # chars: no note needed for a shorter unheard remainder (the sentence end was already clear)


def split_heard(text: str, fraction: float) -> tuple[str, str]:
    """Split the text into heard/unheard parts by the fraction of audio played (0..1); never splits mid-word."""
    text = " ".join((text or "").split())
    fraction = max(0.0, min(1.0, fraction))
    cut = int(len(text) * fraction)
    if cut >= len(text):
        return text, ""
    space = text.rfind(" ", 0, cut + 1)
    cut = space if space > 0 else 0
    return text[:cut].strip(), text[cut:].strip()


def cut_note(heard: str | None, unheard: str) -> str:
    """Note sent to the model ("" = not needed). heard None: the heard part is unknown (the start of the reply)."""
    if len(unheard) < MIN_UNHEARD:
        return ""
    said = "" if heard is None else f'Duyduğu kısım: "{heard[-300:]}". ' if heard else "Cevabından hiçbir şey duymadı. "
    return ("[Not: kullanıcı sözünü kesti. " + said + f'Duymadığı kısım: "{unheard[:600]}". '
            "Yeni söylediğine cevap ver; \"devam et\" derse duymadığı kısımdan sürdür, duyduğunu tekrarlama.]")
