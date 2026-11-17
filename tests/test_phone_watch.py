from __future__ import annotations

from alice.home.phone_watch import PhoneWatch


def n(app, title, text):
    return {"app": app, "title": title, "text": text}


def test_messages_spoken_others_batched():
    sent = []
    mode = ["normal"]
    w = PhoneWatch(lambda x: sent.append(x.body) or True, mode=lambda: mode[0], read=lambda: [], clock=lambda: 0.0)
    old = n("com.whatsapp", "Eski", "dünkü mesaj")
    assert w.handle([old], 0) == []  # first read: existing ones are not spoken
    wa = n("com.whatsapp", "Ahmet", "akşam geliyor musun?")
    assert w.handle([old, wa], 10) == ["WhatsApp, Ahmet: akşam geliyor musun?"]
    assert w.handle([old, wa], 30) == []  # the same notification is not spoken again
    wa2 = n("com.whatsapp", "Grup", "selam")
    assert w.handle([wa, wa2], 35) == []  # same app before 30 sec pass: batched
    others = [n("com.instagram.android.x", "a", str(i)) for i in range(2)] + [n("com.google.android.gm", "Okul", "x")]
    mode[0] = "flight"
    assert w.handle(others, 4000) == []  # in-game, the batched summary waits
    mode[0] = "normal"
    said = w.handle(others + [n("com.android.systemui", "USB", "şarj")], 4100)
    assert said == ["Telefonda 4 bildirim birikti: X 2, WhatsApp 1, Gmail 1."]
    assert w.pending == {}
