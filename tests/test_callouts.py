"""Flight warning audio is pre-cached: in flight (while audio is still coming from the cloud) it plays instantly."""

from __future__ import annotations

import inspect

from alice.sims import flight
from alice.voice.audio import Audio
from alice.voice.speaker import Speaker
from alice.voice.tts import SpeechCache, TTSRouter


class Engine:
    name = "bulut"

    def __init__(self, fail_after: int = 99):
        self.made: list[tuple[str, str]] = []
        self.fail_after = fail_after

    def synth(self, text, emotion):
        if len(self.made) >= self.fail_after:
            raise RuntimeError("kota")
        self.made.append((text, emotion))
        return Audio(b"\x01\x00" * 100, 24000)


class Player:
    def play(self, audio):
        pass

    def stop(self):
        pass


def test_sabit_uyarilar_kodda_soylenenlerle_ayni():
    src = inspect.getsource(flight)
    items = flight.callouts()
    assert len(items) > 20 and items[0][0].startswith("Yere yaklaşıyorsun")
    for text, _ in items[:4]:
        assert f'"{text}"' in src  # if the sentence changes in the code, it won't match the cached one


def test_onceden_uretilir_sinirli_ve_tekrar_uretilmez(tmp_path):
    eng = Engine()
    router = TTSRouter(eng, None, policy=lambda: {}, cache=SpeechCache(tmp_path))
    sp = Speaker(router, Player())
    items = flight.callouts()
    assert sp.prewarm(items, limit=3, background=False) == 3
    assert router.cached(items[0][0], items[0][1]) and not router.cached(items[0][0], "calm")
    assert sp.prewarm(items, limit=3, background=False) == 3  # first three are already cached: the next three
    assert len(eng.made) == 6 and len(set(eng.made)) == 6


def test_ucusta_ve_kota_dolunca_durur(tmp_path):
    eng = Engine(fail_after=1)
    router = TTSRouter(eng, None, policy=lambda: {}, cache=SpeechCache(tmp_path))
    sp = Speaker(router, Player())
    assert sp.prewarm(flight.callouts(), limit=5, background=False) == 1
    flying = TTSRouter(Engine(), None, policy=lambda: {"local_gpu": False}, cache=SpeechCache(tmp_path / "b"))
    assert Speaker(flying, Player()).prewarm(flight.callouts(), background=False) == 0
