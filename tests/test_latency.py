"""Every turn of voice mode is measured; a weekly summary is produced."""

from __future__ import annotations

import json
import threading
import time

from alice.voice import latency


def test_kayit_ve_ozet():
    assert latency.summary() == ""
    for s in (1.2, 1.5, 2.0, 9.0):
        latency.record("canlı", s, model="m", tools=s > 5)
    latency.record("klasik", 4.0)
    text = latency.summary()
    assert "canlı: 4 tur, ortanca 1.8 sn" in text and "en uzun 9.0 sn" in text
    assert "araçlı turlar ortanca 9.0" in text and "klasik: 1 tur" in text
    row = json.loads(latency.log_path().read_text(encoding="utf-8").splitlines()[0])
    assert row["kind"] == "canlı" and "tools" not in row  # empty fields are not written


def test_haftalik_rapor_haftada_bir():
    latency.record("canlı", 2.0)
    now = time.time()
    assert latency.weekly_report(now) == ""  # first start: let a week accumulate
    assert latency.weekly_report(now + 86400) == ""
    assert "canlı: 1 tur" in latency.weekly_report(now + 8 * 86400)
    assert latency.weekly_report(now + 9 * 86400) == ""


def test_klasik_yolda_ilk_ses_olculur():
    from alice.voice.speaker import Speaker

    played = threading.Event()

    class Player:
        def play(self, audio):
            played.set()

        def stop(self):
            pass

    class TTS:
        def synth(self, text, emotion):
            return text

    sp = Speaker(TTS(), Player())
    got = []
    sp.expect(time.monotonic() - 1.0, got.append)
    sp.say("Merhaba, buradayım.")
    assert played.wait(5) and sp.wait(5)
    assert len(got) == 1 and 1.0 <= got[0] < 5
    sp.say("İkinci cümle burada.")
    sp.wait(5)
    assert len(got) == 1  # only the first audio


def test_canli_turun_gecikmesi_kaydedilir():
    from types import SimpleNamespace

    from alice.voice.live import LiveSession

    s = LiveSession.__new__(LiveSession)
    s.model = "gemini-live"
    s._latency(2.5, {"filled": 3.0, "asked": 3.0}, SimpleNamespace(tools=["weather(...)"]))
    row = latency.load()[-1]
    assert row["kind"] == "canlı" and row["tools"] and row["filler"] and row["model"] == "gemini-live"
