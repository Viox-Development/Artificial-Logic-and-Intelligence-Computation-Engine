"""Was the follow-up sentence, said without "Alice" on Discord/in the game, addressed to ALICE? (with Jev)

The user also talks to friends with their own voice; the audio trace cannot tell the two apart, and the live
model did not follow the "stay silent unless it was addressed to you" instruction either (it answered sentences
said to friends such as "Bilemiyorum" (I can't tell) and "Ne olacaksa?" (whatever will be), on 7 November).
Before the live model produces a reply, the transcript is checked with Jev; the decision lives in code:

  p >= ADDRESSED   -> reply
  p <  ADDRESSED   -> stay silent (the session closes quietly)
  Jev unreachable  -> reply (old behaviour, so the conversation doesn't lock up) and log it
"""

from __future__ import annotations

from typing import Any, Callable

from alice.decider import Noul

SETTING = ("The user is playing an online game while talking with friends on Discord voice chat. ALICE, the user's "
           "voice assistant, hears everything the user says into the microphone. ALICE just finished speaking "
           "(`alice_last_reply`), and the user said `utterance` within a few seconds, without saying ALICE's name.")

QUESTION = Noul(
    "Is `utterance` said to ALICE?",
    {"true": "The user answers ALICE's last reply or question, asks ALICE something, or gives ALICE a task.",
     "false": "The user is talking to friends or to themselves, for example about the game or what a teammate "
              "did, and the sentence does not follow from ALICE's last reply."})

# TypeSafe's default "yes" threshold. 0.3-0.7 is "unclear": staying silent on Discord is better than answering a
# sentence said to a friend. In testing, sentences addressed to ALICE scored 0.84-0.93, friend-directed ones 0.05-0.39.
ADDRESSED = 0.7


def build_state(utterance: str, alice_last_reply: str) -> dict[str, Any]:
    return {"setting": SETTING, "alice_last_reply": alice_last_reply.strip()[:600] or "(none)",
            "utterance": utterance.strip()[:600]}


def is_addressed(config, utterance: str, alice_last_reply: str = "", engine: Any = None,
                 log: Callable[[str], None] = lambda s: None) -> tuple[bool, float | None]:
    """(should it be answered, probability). A probability of None means Jev could not be asked."""
    if not utterance.strip():
        return False, None
    if engine is None:
        from alice.providers.jev_route import _get_engine

        engine = _get_engine(config)
    if engine is None:
        return True, None
    try:
        d = engine.decide(build_state(utterance, alice_last_reply), {"addressed": QUESTION})
        p = float(d["addressed"].value)
    except Exception as e:  # noqa: BLE001 - an error is not an answer: don't lock up the conversation
        log(f"hitap kontrolü başarısız ({str(e)[:120]}), cevap veriliyor")
        return True, None
    return p >= ADDRESSED, p
