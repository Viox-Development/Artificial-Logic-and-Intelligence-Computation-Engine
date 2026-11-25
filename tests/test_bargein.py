"""After an interruption ALICE knows the unheard part of its reply; on "devam et" (continue) it picks up there."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from alice.voice.bargein import cut_note, split_heard


def test_split_heard_kelime_ortasindan_bolmez():
    text = "Ankara'da hava kapalı. Akşam yağmur bekleniyor, şemsiye almayı unutma."
    heard, unheard = split_heard(text, 0.4)
    assert text.startswith(heard) and unheard and not unheard.startswith(" ")
    assert heard + " " + unheard == text
    assert split_heard(text, 1.2) == (text, "")
    assert split_heard(text, 0.0) == ("", text)
    assert cut_note("Ankara", "kısa") == ""  # the unheard part is very short: no note needed
    note = cut_note("Ankara'da hava kapalı.", "Akşam yağmur bekleniyor, şemsiye almayı unutma.")
    assert "Part they heard" in note and "şemsiye" in note and "devam et" in note


def test_canli_modda_duyulmayan_kisim_modele_not_olarak_gider():
    from alice.voice.live import LiveSession

    s = LiveSession.__new__(LiveSession)
    s.log = lambda m: None
    sent = []
    ws = SimpleNamespace(send=lambda raw: sent.append(json.loads(raw)))
    said = "Ankara'da hava kapalı ve on iki derece. Akşama doğru yağmur bekleniyor, şemsiye almayı unutma."
    state = {"said": [said], "recv": 1000, "play0": 100, "mute": False}
    s._note_cut(ws, state, SimpleNamespace(played=400))  # 30% of the answer had been played
    content = sent[0]["clientContent"]
    assert content["turnComplete"] is False
    note = content["turns"][0]["parts"][0]["text"]
    assert "şemsiye" in note.split("Part they did not hear")[1]
    assert state["said"][0].endswith("(interrupted)") and "şemsiye" not in state["said"][0]


def test_klasik_yolda_soylenemeyen_cumleler_cevaba_eklenir():
    from alice.voice.assistant import VoiceAssistant
    from alice.voice.speaker import Speaker

    started, release = threading.Event(), threading.Event()

    class Player:
        def play(self, audio):
            started.set()
            release.wait(5)

        def stop(self):
            release.set()

    class TTS:
        def synth(self, text, emotion):
            return text

    sp = Speaker(TTS(), Player())
    sp.say("Birinci cümle burada bitiyor.")
    assert started.wait(5)
    sp._synth_q.put((sp._gen, "İkinci cümle hiç söylenmedi ve uzun bir cümle.", "calm"))
    sp._pending += 1
    sp._play_q.put((sp._gen, "Üçüncü cümle de sırada bekliyordu.", "x"))
    sp.stop()
    cut = sp.last_cut
    assert cut["playing"] == "Birinci cümle burada bitiyor."
    assert cut["pending"][0].startswith("Üçüncü") and cut["pending"][-1].startswith("İkinci")

    va = VoiceAssistant.__new__(VoiceAssistant)
    va.agent = SimpleNamespace(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "anlat"},
                                         {"role": "assistant", "content": "Birinci cümle burada bitiyor. Üçüncü..."}])
    va._cut = cut
    va._note_cut()
    last = va.agent.messages[-1]["content"]
    assert "interrupted you" in last and "cut off in the middle" in last and va._cut is None
